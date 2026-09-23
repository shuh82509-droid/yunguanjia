import os
import re
from typing import Any
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import require_user
from .database import get_db
from .models import Asset
from .oss_service import oss_service


router = APIRouter(prefix="/api/muse", tags=["muse"])


class MuseAnalysisCreate(BaseModel):
    asset_id: int = Field(gt=0)


def _clean(value: Any, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _normalize_results(value: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value if isinstance(value, list) else []):
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "index": index + 1,
                "time_range": _clean(item.get("time_range"), 80),
                "scene_description": _clean(item.get("scene_description"), 1200),
                "script_text": _clean(item.get("script_text"), 1600),
                "scene_text": _clean(item.get("scene_text"), 600),
                "camera_angle": _clean(item.get("camera_angle"), 120),
                "shot_size": _clean(item.get("shot_size"), 120),
                "camera_movement": _clean(item.get("camera_movement"), 120),
                "expression_style": _clean(item.get("expression_style"), 200),
            }
        )
    return normalized[:120]


class MuseCutterClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "WIS_CUTTER_BASE_URL", "https://cloud.fandow.com/gpt/marketing-video"
        ).strip().rstrip("/")

    def _request(self, path: str, *, method: str = "GET", json: dict | None = None) -> dict[str, Any]:
        if not re.match(r"^https?://", self.base_url, re.I):
            raise RuntimeError("视频拆解服务尚未配置")
        response = requests.request(method, f"{self.base_url}{path}", json=json, timeout=30)
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(f"视频拆解服务返回异常（HTTP {response.status_code}）") from error
        if not response.ok or str(payload.get("code", "-1")) != "0":
            message = _clean(payload.get("message"), 240) or f"HTTP {response.status_code}"
            failure = RuntimeError(message)
            setattr(failure, "status_code", response.status_code)
            raise failure
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def select(self, video_url: str) -> list[dict[str, Any]]:
        data = self._request(f"/cutter/select?{urlencode({'oss_url': video_url})}")
        return _normalize_results(data.get("results"))

    def submit(self, video_url: str) -> dict[str, Any]:
        return self._request("/cutter/save", method="POST", json={"oss_url": video_url})

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request(f"/cutter/task?{urlencode({'task_id': task_id})}")


muse_cutter = MuseCutterClient()


def _asset_and_url(db: Session, asset_id: int) -> tuple[Asset, str]:
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "视频素材不存在")
    if asset.media_type != "video":
        raise HTTPException(400, "仅支持分析视频素材")
    video_url = oss_service.url_for(asset.object_key)
    if not re.match(r"^https?://", video_url, re.I):
        raise HTTPException(503, "视频素材暂时无法生成可访问地址")
    return asset, video_url


def _success_payload(asset: Asset, task_id: str, reused: bool, results: list[dict[str, Any]]) -> dict:
    transcript = "\n".join(item["script_text"] for item in results if item.get("script_text"))
    return {
        "status": "success",
        "task_id": task_id,
        "asset_id": asset.id,
        "asset_name": asset.filename,
        "reused": reused,
        "results": results,
        "transcript": transcript,
    }


@router.post("/analyses")
def create_analysis(
    payload: MuseAnalysisCreate,
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    asset, video_url = _asset_and_url(db, payload.asset_id)
    try:
        existing = muse_cutter.select(video_url)
        if existing:
            return _success_payload(asset, "", True, existing)
        submitted = muse_cutter.submit(video_url)
    except RuntimeError as error:
        status_code = int(getattr(error, "status_code", 502) or 502)
        raise HTTPException(status_code if status_code in {409, 429, 503} else 502, str(error)) from error
    task_id = _clean(submitted.get("task_id"), 160)
    if not task_id:
        raise HTTPException(502, "视频拆解服务没有返回任务编号")
    if submitted.get("status") == "success":
        results = muse_cutter.select(video_url)
        return _success_payload(asset, task_id, bool(submitted.get("reused")), results)
    return {
        "status": _clean(submitted.get("status"), 40) or "pending",
        "task_id": task_id,
        "asset_id": asset.id,
        "asset_name": asset.filename,
        "reused": bool(submitted.get("reused")),
        "results": [],
        "transcript": "",
    }


@router.get("/analyses/{task_id}")
def analysis_status(
    task_id: str,
    asset_id: int = Query(gt=0),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,180}", task_id):
        raise HTTPException(400, "任务编号格式无效")
    asset, video_url = _asset_and_url(db, asset_id)
    try:
        task = muse_cutter.task(task_id)
        status = _clean(task.get("status"), 40) or "processing"
        if status == "success":
            results = muse_cutter.select(video_url)
            if not results:
                raise HTTPException(502, "拆解任务已完成，但结果仍为空")
            return _success_payload(asset, task_id, bool(task.get("duplicated")), results)
        if status == "failed":
            return {
                "status": "failed",
                "task_id": task_id,
                "asset_id": asset.id,
                "asset_name": asset.filename,
                "error": _clean(task.get("error_message") or task.get("message"), 300) or "视频拆解失败",
                "results": [],
                "transcript": "",
            }
        return {
            "status": status,
            "task_id": task_id,
            "asset_id": asset.id,
            "asset_name": asset.filename,
            "results": [],
            "transcript": "",
        }
    except HTTPException:
        raise
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error


@router.get("/assets/{asset_id}/analysis")
def existing_analysis(
    asset_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    asset, video_url = _asset_and_url(db, asset_id)
    try:
        results = muse_cutter.select(video_url)
    except RuntimeError as error:
        raise HTTPException(502, str(error)) from error
    if not results:
        return {
            "status": "missing",
            "task_id": "",
            "asset_id": asset.id,
            "asset_name": asset.filename,
            "results": [],
            "transcript": "",
        }
    return _success_payload(asset, "", True, results)
