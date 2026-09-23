from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any

from .config import settings
from .personal_sales_service import FandowDataMcpClient, PersonalSalesError


SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


class RootMaterialUploadError(RuntimeError):
    pass


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _douyin_coverage_sql(day: date, table: str, source: str) -> str:
    value = day.isoformat()
    return f"""SELECT '{source}' AS source_name,
  COUNT(*) AS source_rows,
  COUNT(DISTINCT advertiser_id, material_id) AS observed_materials,
  COUNT(DISTINCT CASE WHEN material_online_time IS NOT NULL THEN advertiser_id END,
    CASE WHEN material_online_time IS NOT NULL THEN material_id END) AS online_time_materials,
  COUNT(DISTINCT CASE
    WHEN material_online_time >= TIMESTAMP('{value} 00:00:00')
      AND material_online_time < TIMESTAMP('{(day + timedelta(days=1)).isoformat()} 00:00:00')
    THEN advertiser_id END,
    CASE
      WHEN material_online_time >= TIMESTAMP('{value} 00:00:00')
        AND material_online_time < TIMESTAMP('{(day + timedelta(days=1)).isoformat()} 00:00:00')
      THEN material_id END) AS confirmed_materials,
  MAX(updated_at) AS source_updated_at
FROM {table}
WHERE report_date = DATE('{value}')
  AND material_id IS NOT NULL"""


def _wechat_upload_sql(day: date) -> str:
    value = day.isoformat()
    following = (day + timedelta(days=1)).isoformat()
    return f"""SELECT
  COUNT(*) AS source_rows,
  COUNT(DISTINCT finder_account_id, feed_id) AS observed_materials,
  COUNT(DISTINCT CASE
    WHEN published_at >= TIMESTAMP('{value} 00:00:00')
      AND published_at < TIMESTAMP('{following} 00:00:00')
    THEN finder_account_id END,
    CASE
      WHEN published_at >= TIMESTAMP('{value} 00:00:00')
        AND published_at < TIMESTAMP('{following} 00:00:00')
      THEN feed_id END) AS confirmed_materials,
  MAX(updated_at) AS source_updated_at
FROM d_root_project_mid_db_brandmarketing.wechat_compass_short_video_detail_daily
WHERE stat_date = DATE('{value}')
  AND feed_id <> ''"""


class RootMaterialUploadService:
    """Read complete-day material online/publish counts from FanDo root data only."""

    _DOUYIN_TABLES = (
        (
            "d_root_project_mid_db_brandmarketing.qianchuan_ad_plan_hourly_delivery_cost_detail",
            "QIANCHUAN_STANDARD",
        ),
        (
            "d_root_project_mid_db_brandmarketing.qianchuan_chengfang_ad_plan_hourly_delivery_cost_detail",
            "QIANCHUAN_CHENGFANG_PLAN",
        ),
        (
            "d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report",
            "QIANCHUAN_CHENGFANG_MATERIAL",
        ),
    )

    def __init__(self) -> None:
        self._lock = Lock()
        self._cache: dict[str, tuple[datetime, dict[str, Any]]] = {}

    @property
    def configured(self) -> bool:
        return bool(settings.fandow_data_mcp_token and settings.fandow_data_mcp_endpoint)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "source": "FanDo 根数据",
            "preservesMissingAsNull": True,
            "cacheSeconds": 300,
        }

    def get_snapshot(self, target_date: date) -> dict[str, Any]:
        cache_key = target_date.isoformat()
        now = datetime.now(SHANGHAI_TZ)
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]).total_seconds() < 300:
            return json.loads(json.dumps(cached[1], ensure_ascii=False))
        if not self.configured:
            raise RootMaterialUploadError("FanDo 根数据连接尚未配置，素材上线数量不可用")
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and (now - cached[0]).total_seconds() < 300:
                return json.loads(json.dumps(cached[1], ensure_ascii=False))
            try:
                client = FandowDataMcpClient(
                    settings.fandow_data_mcp_endpoint,
                    settings.fandow_data_mcp_token,
                    settings.fandow_data_mcp_timeout_seconds,
                )
                client.initialize()
                douyin_rows: list[dict[str, Any]] = []
                for table, source in self._DOUYIN_TABLES:
                    rows, capped = client.run_sql(
                        f"抖店素材上线覆盖 {source}",
                        _douyin_coverage_sql(target_date, table, source),
                        10,
                    )
                    if capped:
                        raise RootMaterialUploadError(f"{source} 覆盖查询结果异常截断")
                    douyin_rows.extend(rows)
                wechat_rows, capped = client.run_sql(
                    "视频号素材发布数量",
                    _wechat_upload_sql(target_date),
                    10,
                )
                if capped:
                    raise RootMaterialUploadError("视频号素材发布查询结果异常截断")
            except (PersonalSalesError, RootMaterialUploadError) as error:
                raise RootMaterialUploadError(str(error)) from error
            except Exception as error:
                raise RootMaterialUploadError("FanDo 根数据素材上线查询失败") from error

            douyin_source_rows = sum(_integer(row.get("source_rows")) for row in douyin_rows)
            observed = sum(_integer(row.get("observed_materials")) for row in douyin_rows)
            online_time_materials = sum(_integer(row.get("online_time_materials")) for row in douyin_rows)
            confirmed = sum(_integer(row.get("confirmed_materials")) for row in douyin_rows)
            douyin_updated_at = max(
                (str(row.get("source_updated_at") or "") for row in douyin_rows),
                default="",
            ) or None
            coverage_rate = round(online_time_materials / observed, 4) if observed else None
            douyin_ready = douyin_source_rows > 0 and online_time_materials == observed
            douyin_count: int | None = confirmed if douyin_ready else None

            wechat = wechat_rows[0] if wechat_rows else {}
            wechat_source_rows = _integer(wechat.get("source_rows"))
            wechat_observed = _integer(wechat.get("observed_materials"))
            wechat_confirmed = _integer(wechat.get("confirmed_materials"))
            wechat_ready = wechat_source_rows > 0
            snapshot = {
                "schemaVersion": 3,
                "date": cache_key,
                "generatedAt": now.isoformat(timespec="seconds"),
                "status": "ready" if douyin_ready and wechat_ready else "partial",
                "sourceMode": "root-data",
                "definition": (
                    "抖店按素材上线时间与 source+advertiser_id+material_id 去重；"
                    "视频号按发布时间与 finder_account_id+feed_id 去重。根字段缺失时返回待回补，不以 0 或工作台任务数代替。"
                ),
                "channels": [
                    {
                        "key": "douyin",
                        "label": "抖店",
                        "confirmedAssets": douyin_count,
                        "observedAssets": observed,
                        "state": "ready" if douyin_ready else "pending",
                        "updatedAt": douyin_updated_at,
                        "sourceTable": "、".join(table for table, _source in self._DOUYIN_TABLES),
                        "timeField": "material_online_time",
                        "dedupeKey": "source + advertiser_id + material_id",
                        "metadataCoverage": {
                            "onlineTimeMaterials": online_time_materials,
                            "totalMaterials": observed,
                            "rate": coverage_rate,
                        },
                        "note": (
                            None
                            if douyin_ready
                            else (
                                f"根表发现 {observed} 个广告主素材组合，但仅 {online_time_materials} 个已回补上线时间；当前不能按 0 展示。"
                                if douyin_source_rows
                                else "该自然日根表分区未返回数据，当前不能按 0 展示。"
                            )
                        ),
                    },
                    {
                        "key": "wechat",
                        "label": "视频号",
                        "confirmedAssets": wechat_confirmed if wechat_ready else None,
                        "observedAssets": wechat_observed,
                        "state": "ready" if wechat_ready else "pending",
                        "updatedAt": str(wechat.get("source_updated_at") or "") or None,
                        "sourceTable": "d_root_project_mid_db_brandmarketing.wechat_compass_short_video_detail_daily",
                        "timeField": "published_at",
                        "dedupeKey": "finder_account_id + feed_id",
                        "metadataCoverage": {
                            "onlineTimeMaterials": wechat_observed,
                            "totalMaterials": wechat_observed,
                            "rate": 1 if wechat_observed else None,
                        },
                        "note": None if wechat_ready else "该自然日根表分区未返回数据，当前不能按 0 展示。",
                    },
                ],
            }
            self._cache[cache_key] = (now, snapshot)
            return json.loads(json.dumps(snapshot, ensure_ascii=False))


root_material_upload_service = RootMaterialUploadService()
