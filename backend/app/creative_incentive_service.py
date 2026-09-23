from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CreativeIncentiveDirection, CreativeIncentiveMilestone


POINT_GMV_YUAN = 20_000


def _money(value: float | None) -> str:
    return "待核验" if value is None else f"{value:,.2f}"


def _roi(value: float | None) -> str:
    return "待核验" if value is None else f"{value:.2f}"


def _share_date() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    return f"{now.year}年{now.month}月{now.day}日"


def compose_share_text(direction: CreativeIncentiveDirection, point_number: int) -> str:
    gmv = direction.latest_gmv_yuan
    cost = direction.latest_cost_yuan
    roi = direction.latest_roi
    cutoff = direction.source_cutoff_at or direction.source_updated_at or "待核验"
    if point_number > 1:
        return (
            f"【原创方向进阶播报】{direction.creator_name}｜{direction.direction_name}\n"
            f"数据结果（截至 {cutoff}）：成交 {_money(gmv)}，ROI {_roi(roi)}，消耗 {_money(cost)}\n"
            f"本次新增：+1分｜累计：{point_number}分（每满2万元计1分）"
        )
    return "\n".join([
        f"分享时间：{_share_date()}",
        f"方向：{direction.direction_name}",
        f"  · 创作者：{direction.creator_name}｜{direction.center or '中心待回补'}",
        f"  · 素材：{direction.material_name or '名称待回补'}（ID {direction.material_id}）",
        f"  · 原创说明：{direction.originality_note or '待补充'}",
        "有效点判断",
        f"  · 文案：{direction.copy_judgement or '待补充'}",
        f"  · 画面：{direction.visual_judgement or '待补充'}",
        f"  · 声音：{direction.voice_judgement or '待补充'}",
        f"  · 后续裂变：{direction.remix_plan or '待补充'}",
        f"数据结果（截至 {cutoff}）：成交 {_money(gmv)}，ROI {_roi(roi)}，消耗 {_money(cost)}",
        "积分：+1分｜累计1分（首次达到2万元）",
        f"参考素材：{direction.reference_url or '待补充'}",
    ])


def sync_directions(
    db: Session,
    directions: list[CreativeIncentiveDirection],
    snapshot: dict[str, Any],
) -> dict[str, int]:
    candidates = snapshot.get("_materialCandidates") or snapshot.get("topMaterials") or []
    material_by_id = {
        str(item.get("materialId") or "").strip(): item
        for item in candidates
        if str(item.get("materialId") or "").strip()
    }
    generated = 0
    matched = 0
    for direction in directions:
        item = material_by_id.get(direction.material_id)
        direction.synced_at = datetime.utcnow()
        direction.updated_at = datetime.utcnow()
        if not item:
            direction.metric_status = "unmatched"
            direction.latest_gmv_yuan = None
            direction.latest_cost_yuan = None
            direction.latest_roi = None
            direction.source_cutoff_at = ""
            direction.source_updated_at = ""
            continue
        matched += 1
        direction.metric_status = "stale" if snapshot.get("status") == "stale" else "ready"
        direction.latest_gmv_yuan = float(item.get("gmvYuan")) if item.get("gmvYuan") is not None else None
        direction.latest_cost_yuan = float(item.get("costYuan")) if item.get("costYuan") is not None else None
        direction.latest_roi = float(item.get("roi")) if item.get("roi") is not None else None
        direction.source_cutoff_at = str(item.get("sourceCutoffAt") or "")[:40]
        direction.source_updated_at = str(item.get("sourceUpdatedAt") or "")[:40]
        if direction.originality_status != "confirmed" or direction.latest_gmv_yuan is None:
            continue
        reached = int(direction.latest_gmv_yuan // POINT_GMV_YUAN)
        existing = set(db.scalars(
            select(CreativeIncentiveMilestone.point_number)
            .where(CreativeIncentiveMilestone.direction_id == direction.id)
        ).all())
        for point_number in range(1, reached + 1):
            if point_number in existing:
                continue
            milestone = CreativeIncentiveMilestone(
                id=str(uuid4()),
                direction_id=direction.id,
                point_number=point_number,
                threshold_gmv_yuan=point_number * POINT_GMV_YUAN,
                observed_gmv_yuan=direction.latest_gmv_yuan,
                observed_cost_yuan=direction.latest_cost_yuan,
                observed_roi=direction.latest_roi,
                source_cutoff_at=direction.source_cutoff_at,
                share_type="full" if point_number == 1 else "update",
            )
            milestone.share_text = compose_share_text(direction, point_number)
            db.add(milestone)
            generated += 1
    db.commit()
    return {"matched": matched, "unmatched": len(directions) - matched, "generated": generated}


def deliver_share_text(text: str) -> tuple[str, str, str]:
    webhook = os.getenv("FEISHU_CREATIVE_INCENTIVE_WEBHOOK_URL", "").strip()
    if not webhook:
        return "unconfigured", "", "创意突破分享群机器人尚未配置，草稿已保留，可复制后人工发布"
    try:
        response = requests.post(
            webhook,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=12,
        )
        payload = response.json() if response.content else {}
        code = payload.get("code", payload.get("StatusCode", 0)) if isinstance(payload, dict) else 0
        if response.status_code >= 400 or code != 0:
            message = payload.get("msg", payload.get("StatusMessage", "飞书群发送失败")) if isinstance(payload, dict) else "飞书群发送失败"
            return "failed", "", f"飞书错误 {code}：{message}"[:1000]
        message_id = ""
        if isinstance(payload, dict):
            message_id = str((payload.get("data") or {}).get("message_id") or "")
        return "sent", message_id, ""
    except Exception as error:
        return "failed", "", str(error)[:1000]


def deliver_pending_milestones(db: Session, limit: int = 20) -> int:
    if not os.getenv("FEISHU_CREATIVE_INCENTIVE_WEBHOOK_URL", "").strip():
        return 0
    rows = db.scalars(
        select(CreativeIncentiveMilestone)
        .where(CreativeIncentiveMilestone.share_status == "draft")
        .order_by(CreativeIncentiveMilestone.created_at)
        .limit(limit)
    ).all()
    delivered = 0
    for item in rows:
        status, message_id, error = deliver_share_text(item.share_text)
        item.delivery_error = error
        if status != "sent":
            continue
        item.share_status = "published"
        item.feishu_message_id = message_id
        item.published_by_name = "素材实时激励系统"
        item.published_at = datetime.utcnow()
        delivered += 1
    db.commit()
    return delivered
