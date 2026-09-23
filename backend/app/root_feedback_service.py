from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Any

from .config import settings
from .personal_sales_service import FandowDataMcpClient, PersonalSalesError


class RootFeedbackError(RuntimeError):
    pass


def _sql_literal(value: Any) -> str:
    return "'" + str(value or "").replace("\\", "\\\\").replace("'", "''") + "'"


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return int(number) if integer else float(number)


def _money_fen(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str:
    text = Path(str(value or "")).stem.casefold()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", text)


class RootFeedbackService:
    """Read-only material feedback from FanDo root data.

    Root rows are accepted only on stable platform identifiers or a unique
    account/time/content match. Missing rows and missing fields stay unknown;
    callers must not coerce them to zero.
    """

    ADQ_TABLE = "d_root_project_mid_db_brandmarketing.adq_video_material_databoard"
    CHANNELS_TABLE = "d_root_project_marketing_db.wechat_compass_short_video_detail_daily"

    def __init__(self) -> None:
        self._lock = Lock()

    @property
    def configured(self) -> bool:
        return bool(settings.fandow_data_mcp_endpoint and settings.fandow_data_mcp_token)

    def _client(self) -> FandowDataMcpClient:
        if not self.configured:
            raise RootFeedbackError("FanDo 根数据连接尚未配置")
        try:
            client = FandowDataMcpClient(
                settings.fandow_data_mcp_endpoint,
                settings.fandow_data_mcp_token,
                settings.fandow_data_mcp_timeout_seconds,
            )
            client.initialize()
            return client
        except (PersonalSalesError, OSError) as error:
            raise RootFeedbackError(str(error)) from error

    @staticmethod
    def _adq_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        cost = sum((_number(row.get("cost_yuan")) or 0) for row in rows)
        order_amount = sum((_number(row.get("order_amount_yuan")) or 0) for row in rows)
        order_net_amount = sum((_number(row.get("order_net_amount_yuan")) or 0) for row in rows)
        return {
            "cost_yuan": round(cost, 2),
            "view_count": sum((_number(row.get("view_count"), integer=True) or 0) for row in rows),
            "valid_click_count": sum((_number(row.get("valid_click_count"), integer=True) or 0) for row in rows),
            "order_amount_yuan": round(order_amount, 2),
            "order_net_amount_yuan": round(order_net_amount, 2),
            "order_roi": round(order_amount / cost, 6) if cost else None,
            "order_net_roi": round(order_net_amount / cost, 6) if cost else None,
            "account_count": max((_number(row.get("account_count"), integer=True) or 0) for row in rows),
        }

    def adq_material_metrics_batch(
        self,
        video_ids: list[str],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(value or "").strip() for value in video_ids if str(value or "").strip()))
        if not normalized_ids:
            return {}
        local_today = (datetime.utcnow() + timedelta(hours=8)).date()
        end = date.fromisoformat(end_date) if end_date else local_today - timedelta(days=1)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=29)
        if start > end:
            raise RootFeedbackError("数据开始日期不能晚于结束日期")
        values = ",".join(_sql_literal(value) for value in normalized_ids)
        sql = f"""SELECT DATE_FORMAT(report_date, '%Y-%m-%d') AS stat_date, video_id,
  MAX(CAST(video_asset_id AS CHAR)) AS material_id,
  COUNT(DISTINCT video_asset_id) AS material_id_count,
  ROUND(SUM(cost_yuan), 2) AS cost_yuan,
  SUM(view_count) AS view_count,
  SUM(valid_click_count) AS valid_click_count,
  ROUND(SUM(order_amount_yuan), 2) AS order_amount_yuan,
  ROUND(SUM(order_net_amount_yuan), 2) AS order_net_amount_yuan,
  COUNT(DISTINCT ad_account_id) AS account_count,
  MAX(updated_at) AS source_updated_at
FROM {self.ADQ_TABLE}
WHERE report_time_line = 'REPORTING_TIME'
  AND report_date BETWEEN DATE({_sql_literal(start.isoformat())}) AND DATE({_sql_literal(end.isoformat())})
  AND video_id IN ({values})
GROUP BY report_date, video_id
ORDER BY report_date, video_id"""
        try:
            with self._lock:
                rows, capped = self._client().run_sql("ADQ 素材根数据", sql, limit=10000)
        except (PersonalSalesError, OSError) as error:
            raise RootFeedbackError(str(error)) from error
        if capped:
            raise RootFeedbackError("ADQ 素材根数据结果超过安全上限，未写入不完整结果")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("video_id") or ""), []).append(row)
        result: dict[str, dict[str, Any]] = {}
        for video_id in normalized_ids:
            video_rows = grouped.get(video_id, [])
            material_ids = sorted({
                str(row.get("material_id") or "").strip()
                for row in video_rows
                if str(row.get("material_id") or "").strip()
            })
            material_id_ambiguous = (
                len(material_ids) > 1
                or any((_number(row.get("material_id_count"), integer=True) or 0) > 1 for row in video_rows)
            )
            material_id = material_ids[0] if len(material_ids) == 1 and not material_id_ambiguous else ""
            material_id_status = "verified" if material_id else ("ambiguous" if material_id_ambiguous else "missing")
            daily = [
                {
                    "date": str(row.get("stat_date") or "")[:10],
                    **self._adq_metrics([row]),
                    "material_id": str(row.get("material_id") or ""),
                    "has_data": True,
                    "source": "fandow_root",
                }
                for row in video_rows
            ]
            result[video_id] = {
                "metrics": self._adq_metrics(video_rows) if video_rows else {},
                "daily": daily,
                "has_data": bool(video_rows),
                "row_count": len(video_rows),
                "material_id": material_id,
                "material_id_status": material_id_status,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "source": "fandow_root.adq_video_material_databoard",
                "message": (
                    (
                        f"已从根数据按视频 ID 确认素材 ID {material_id}，"
                        f"并汇总 {len(video_rows)} 天、跨账户素材数据"
                    )
                    if video_rows and material_id
                    else (
                        "根数据返回了多个素材 ID，暂不自动选择；请人工核验"
                        if video_rows and material_id_ambiguous
                        else f"已从根数据按视频 ID 汇总 {len(video_rows)} 天、跨账户素材数据，素材 ID 待核验"
                    )
                    if video_rows
                    else "根数据在近 30 日未返回该视频记录；这是暂无数据，不按 0 处理"
                ),
            }
        return result

    @staticmethod
    def _channels_match_score(item: dict[str, Any], row: dict[str, Any]) -> tuple[float, bool]:
        anchor = item.get("anchor_local")
        published = _parse_datetime(row.get("published_at"))
        if not isinstance(anchor, datetime) or published is None:
            return 0.0, False
        delta_minutes = abs((published - anchor).total_seconds()) / 60
        if delta_minutes > 360:
            return 0.0, False
        expected_ids = {
            str(item.get("platform_export_id") or "").strip(),
            str(item.get("platform_content_id") or "").strip(),
        } - {""}
        exact_id = str(row.get("export_id") or "").strip() in expected_ids
        if exact_id:
            return 220 - min(delta_minutes, 120) / 4, True
        if delta_minutes > 120:
            return 0.0, False
        root_text = _normalize_text(row.get("video_name"))
        signals = [
            (_normalize_text(item.get("description")), 105.0, 82.0),
            (_normalize_text(item.get("title")), 95.0, 74.0),
            (_normalize_text(item.get("filename")), 75.0, 60.0),
        ]
        text_score = 0.0
        for expected, exact, contains in signals:
            if not expected or not root_text:
                continue
            if expected == root_text:
                text_score = max(text_score, exact)
            elif min(len(expected), len(root_text)) >= 6 and (expected in root_text or root_text in expected):
                text_score = max(text_score, contains)
        time_score = max(0.0, 60.0 - delta_minutes * 5)
        return text_score + time_score, False

    def channels_delivery_batch(self, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for raw in items:
            anchor_source = (
                "published_at" if raw.get("published_at")
                else "submitted_at" if raw.get("submitted_at")
                else "created_at"
            )
            anchor = raw.get(anchor_source)
            if not isinstance(anchor, datetime) or not str(raw.get("account_name") or "").strip():
                continue
            prepared.append({**raw, "anchor_local": anchor + timedelta(hours=8), "anchor_source": anchor_source})
        if not prepared:
            return {}
        start = min(item["anchor_local"].date() for item in prepared) - timedelta(days=1)
        end = max(item["anchor_local"].date() for item in prepared) + timedelta(days=1)
        account_values = ",".join(
            _sql_literal(value)
            for value in sorted({str(item["account_name"]).strip() for item in prepared})
        )
        sql = f"""SELECT DATE_FORMAT(stat_date, '%Y-%m-%d') AS stat_date,
  finder_account_name, finder_account_id, feed_id, export_id, published_at,
  video_name, play_count, thumb_like_count, heart_like_count, pay_gmv_yuan, updated_at
FROM {self.CHANNELS_TABLE}
WHERE stat_date BETWEEN DATE({_sql_literal(start.isoformat())}) AND DATE({_sql_literal(end.isoformat())})
  AND finder_account_name IN ({account_values})
ORDER BY published_at"""
        try:
            with self._lock:
                rows, capped = self._client().run_sql("视频号主页根数据", sql, limit=10000)
        except (PersonalSalesError, OSError) as error:
            raise RootFeedbackError(str(error)) from error
        if capped:
            raise RootFeedbackError("视频号主页根数据结果超过安全上限，未写入不完整结果")

        candidates: dict[str, list[tuple[float, bool, dict[str, Any]]]] = {}
        for item in prepared:
            task_id = str(item.get("id") or "")
            account_name = str(item.get("account_name") or "").strip()
            scored = []
            for row in rows:
                if str(row.get("finder_account_name") or "").strip() != account_name:
                    continue
                score, exact_id = self._channels_match_score(item, row)
                if score >= 100:
                    scored.append((score, exact_id, row))
            scored.sort(key=lambda value: value[0], reverse=True)
            candidates[task_id] = scored

        assignments: dict[str, dict[str, Any]] = {}
        used_feeds: set[str] = set()
        task_order = sorted(
            prepared,
            key=lambda item: candidates.get(str(item.get("id") or ""), [(0, False, {})])[0][0]
            if candidates.get(str(item.get("id") or "")) else 0,
            reverse=True,
        )
        for item in task_order:
            task_id = str(item.get("id") or "")
            scored = [entry for entry in candidates.get(task_id, []) if str(entry[2].get("feed_id") or "") not in used_feeds]
            if not scored:
                continue
            top_score, exact_id, row = scored[0]
            ambiguity_margin = 10 if item.get("anchor_source") == "created_at" else 5
            if not exact_id and len(scored) > 1 and top_score - scored[1][0] < ambiguity_margin:
                continue
            feed_id = str(row.get("feed_id") or "").strip()
            if not feed_id:
                continue
            used_feeds.add(feed_id)
            like_parts = [
                _number(row.get("thumb_like_count"), integer=True),
                _number(row.get("heart_like_count"), integer=True),
            ]
            like_count = sum(value for value in like_parts if value is not None) if any(value is not None for value in like_parts) else None
            assignments[task_id] = {
                "found": True,
                "published": True,
                "publication_state": "published",
                "platform_content_id": feed_id,
                "platform_export_id": str(row.get("export_id") or "").strip(),
                "platform_export_source": "fandow_root",
                "view_count": _number(row.get("play_count"), integer=True),
                "like_count": like_count,
                "comment_count": None,
                "share_count": None,
                "order_count": None,
                "gmv_fen": _money_fen(row.get("pay_gmv_yuan")),
                "metrics_date": str(row.get("stat_date") or "")[:10],
                "published_at": str(row.get("published_at") or ""),
                "message": "已由根数据确认视频号公开发布，并回流播放、点赞与成交金额；缺失字段保持待回流",
                "source": "fandow_root.wechat_compass_short_video_detail_daily",
                "match_score": round(top_score, 2),
            }
        return assignments


root_feedback_service = RootFeedbackService()
