from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import requests

from .config import settings


SHANGHAI_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
DATA_DIR = Path(__file__).resolve().parent / "data"
PEOPLE_PATH = DATA_DIR / "creator_keywords.json"
SEED_SNAPSHOT_PATH = DATA_DIR / "personal_sales_snapshot.seed.json"


class PersonalSalesError(RuntimeError):
    pass


class SourcePendingError(PersonalSalesError):
    pass


def _round2(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def _keyword_matches(signal: str, keyword: str) -> bool:
    if len(keyword) < 2:
        return False
    if re.fullmatch(r"[\u3400-\u9fff]+", keyword):
        return keyword in signal
    return bool(re.search(rf"(^|[^a-z0-9]){re.escape(keyword)}(?=$|[^a-z])", signal, re.IGNORECASE))


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _source_key(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(field) or "")
        for field in ("channel", "source_platform", "advertiser_id", "material_id")
    )


def _material_dedup_key(material: dict[str, Any]) -> str:
    normalized_title = re.sub(
        r"[^a-z0-9\u3400-\u9fff]+",
        "",
        _normalize(material.get("materialName")),
    )
    return normalized_title or f"{material.get('sourcePlatform', '')}|{material.get('materialId', '')}"


class FandowDataMcpClient:
    def __init__(self, endpoint: str, token: str, timeout_seconds: int = 120):
        self.endpoint = endpoint.rstrip("/") + "/"
        self.token = token.strip()
        self.timeout_seconds = timeout_seconds
        self.session_id = ""
        self.next_id = 1
        self.http = requests.Session()
        if not self.token:
            raise PersonalSalesError("FanDo 根数据连接尚未配置")

    @staticmethod
    def _parse_payload(text: str, content_type: str) -> dict[str, Any] | None:
        if "text/event-stream" in content_type:
            payloads = []
            for line in text.splitlines():
                if line.startswith("data:") and line[5:].strip():
                    payloads.append(json.loads(line[5:].strip()))
            return payloads[-1] if payloads else None
        return json.loads(text) if text.strip() else None

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = self.http.post(
            self.endpoint,
            headers=headers,
            json={"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}},
            timeout=self.timeout_seconds,
        )
        self.next_id += 1
        response.raise_for_status()
        if response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        payload_text = response.content.decode("utf-8")
        return self._parse_payload(payload_text, response.headers.get("content-type", ""))

    def initialize(self) -> None:
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "wis-personal-sales-dashboard", "version": "1.0.0"},
            },
        )
        self.rpc("notifications/initialized", {})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        return (response.get("result") or {}).get("structuredContent") or {}

    def run_sql(self, label: str, sql: str, limit: int = 10000) -> tuple[list[dict[str, Any]], bool]:
        validation = self.call_tool("validate_sql", {"sql": sql})
        if validation.get("ok") is not True or (validation.get("data") or {}).get("valid") is not True:
            reason = (validation.get("error") or {}).get("message") or (validation.get("data") or {}).get("reason")
            raise PersonalSalesError(f"{label}查询校验失败：{reason or '未知原因'}")
        result = self.call_tool("execute_sql", {"sql": sql, "limit": limit})
        if result.get("ok") is not True:
            reason = (result.get("error") or {}).get("message") or "未知原因"
            raise PersonalSalesError(f"{label}查询失败：{reason}")
        rows = (result.get("data") or {}).get("rows") or []
        return rows, len(rows) >= limit


def _qianchuan_bounds(day: date) -> tuple[str, str]:
    following = day + timedelta(days=1)
    return f"{day.isoformat()} 23:30:00", f"{following.isoformat()} 00:00:00"


def _standard_qianchuan_sql(day: date) -> str:
    start, end = _qianchuan_bounds(day)
    value = day.isoformat()
    return f"""WITH latest_snapshot AS (
  SELECT advertiser_id, MAX(stat_time) AS stat_time
  FROM d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report FORCE INDEX (uk_qc_material_hourly)
  WHERE report_date = DATE('{value}')
    AND stat_time BETWEEN TIMESTAMP('{start}') AND TIMESTAMP('{end}')
  GROUP BY advertiser_id
)
SELECT '{value}' AS report_date, '千川' AS channel, 'STANDARD' AS source_platform,
  m.advertiser_id, m.material_id,
  MAX(COALESCE(NULLIF(a.platform_material_name, ''), NULLIF(a.filename, ''), NULLIF(m.content_description, ''))) AS material_name,
  MAX(NULLIF(a.internal_author, '')) AS internal_author,
  ROUND(SUM(COALESCE(m.total_pay_order_gmv_include_coupon_for_roi2, m.total_pay_order_gmv_for_roi2, 0)), 2) AS gmv_yuan,
  MAX(m.stat_time) AS source_cutoff_at, MAX(m.updated_at) AS source_updated_at
FROM latest_snapshot s
JOIN d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report m FORCE INDEX (uk_qc_material_hourly)
  ON m.report_date = DATE('{value}') AND m.advertiser_id = s.advertiser_id AND m.stat_time = s.stat_time
LEFT JOIN d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger a
  ON a.advertiser_id = m.advertiser_id AND a.material_id = m.material_id
GROUP BY m.advertiser_id, m.material_id
HAVING gmv_yuan > 0"""


def _chengfang_qianchuan_sql(day: date) -> str:
    start, end = _qianchuan_bounds(day)
    value = day.isoformat()
    return f"""WITH latest_snapshot AS (
  SELECT advertiser_id, MAX(stat_time) AS stat_time
  FROM d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report FORCE INDEX (uk_qc_material_hourly)
  WHERE report_date = DATE('{value}')
    AND stat_time BETWEEN TIMESTAMP('{start}') AND TIMESTAMP('{end}')
  GROUP BY advertiser_id
)
SELECT '{value}' AS report_date, '千川' AS channel, 'CHENGFANG' AS source_platform,
  m.advertiser_id, m.material_id,
  MAX(COALESCE(NULLIF(m.material_name, ''), NULLIF(m.content_description, ''))) AS material_name,
  NULL AS internal_author,
  ROUND(SUM(COALESCE(m.total_pay_order_gmv_include_coupon_for_roi2, m.total_pay_order_gmv_for_roi2, 0)), 2) AS gmv_yuan,
  MAX(m.stat_time) AS source_cutoff_at, MAX(m.updated_at) AS source_updated_at
FROM latest_snapshot s
JOIN d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report m FORCE INDEX (uk_qc_material_hourly)
  ON m.report_date = DATE('{value}') AND m.advertiser_id = s.advertiser_id AND m.stat_time = s.stat_time
GROUP BY m.advertiser_id, m.material_id
HAVING gmv_yuan > 0"""


def _haitun_sql(day: date) -> str:
    value = day.isoformat()
    return f"""WITH latest_snapshot AS (
  SELECT platform_account_id, MAX(collection_time) AS collection_time
  FROM d_root_project_mid_db_brandmarketing.haitun_zhitou_short_video_material_hourly_detail
  WHERE stat_date = DATE('{value}')
  GROUP BY platform_account_id
)
SELECT '{value}' AS report_date, '视频号' AS channel, 'HAITUN' AS source_platform,
  m.platform_account_id AS advertiser_id,
  COALESCE(NULLIF(m.platform_video_id, ''), m.material_record_key) AS material_id,
  MAX(COALESCE(NULLIF(m.material_title, ''), NULLIF(m.material_remark, ''))) AS material_name,
  NULL AS internal_author,
  ROUND(MAX(COALESCE(m.transaction_amount_yuan, 0)), 2) AS gmv_yuan,
  MAX(TIMESTAMP(m.stat_date, m.collection_time)) AS source_cutoff_at, MAX(m.updated_at) AS source_updated_at
FROM latest_snapshot s
JOIN d_root_project_mid_db_brandmarketing.haitun_zhitou_short_video_material_hourly_detail m
  ON m.stat_date = DATE('{value}') AND m.platform_account_id = s.platform_account_id AND m.collection_time = s.collection_time
GROUP BY m.platform_account_id, COALESCE(NULLIF(m.platform_video_id, ''), m.material_record_key)
HAVING gmv_yuan > 0"""


def _adq_sql(day: date) -> str:
    value = day.isoformat()
    return f"""WITH ranked AS (
  SELECT report_date, ad_account_id, video_id, video_name, order_amount_yuan, updated_at,
    ROW_NUMBER() OVER (PARTITION BY report_date, ad_account_id, video_id ORDER BY updated_at DESC, id DESC) AS row_rank
  FROM d_root_project_mid_db_brandmarketing.adq_video_material_databoard
  WHERE report_date = DATE('{value}') AND report_time_line = 'REPORTING_TIME'
)
SELECT '{value}' AS report_date, '视频号' AS channel, 'ADQ' AS source_platform,
  ad_account_id AS advertiser_id, video_id AS material_id, video_name AS material_name,
  NULL AS internal_author, ROUND(COALESCE(order_amount_yuan, 0), 2) AS gmv_yuan,
  CONCAT('{value}', ' 23:59:59') AS source_cutoff_at, updated_at AS source_updated_at
FROM ranked
WHERE row_rank = 1 AND COALESCE(order_amount_yuan, 0) > 0"""


def _freshness_sql(target: date) -> str:
    value = target.isoformat()
    return f"""SELECT 'QIANCHUAN_STANDARD' AS source_name, MAX(report_date) AS data_date
FROM d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report WHERE report_date <= DATE('{value}')
UNION ALL
SELECT 'QIANCHUAN_CHENGFANG', MAX(report_date)
FROM d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report WHERE report_date <= DATE('{value}')
UNION ALL
SELECT 'HAITUN', MAX(stat_date)
FROM d_root_project_mid_db_brandmarketing.haitun_zhitou_short_video_material_hourly_detail WHERE stat_date <= DATE('{value}')
UNION ALL
SELECT 'ADQ', MAX(report_date)
FROM d_root_project_mid_db_brandmarketing.adq_video_material_databoard
WHERE report_date <= DATE('{value}') AND report_time_line = 'REPORTING_TIME'"""


def build_snapshot(
    daily_rows: list[dict[str, Any]],
    people: list[dict[str, Any]],
    target_date: date,
    freshness: dict[str, str],
    capped_queries: list[str] | None = None,
    period_start: date | None = None,
    include_period_rows: bool = True,
) -> dict[str, Any]:
    capped_queries = capped_queries or []
    monthly: dict[str, dict[str, Any]] = {}
    for row in daily_rows:
        gmv = _number(row.get("gmv_yuan"))
        if gmv <= 0:
            continue
        key = _source_key(row)
        report_date = str(row.get("report_date") or "")[:10]
        current = monthly.setdefault(
            key,
            {
                "channel": str(row.get("channel") or ""),
                "sourcePlatform": str(row.get("source_platform") or ""),
                "advertiserId": str(row.get("advertiser_id") or ""),
                "materialId": str(row.get("material_id") or ""),
                "materialName": "",
                "internalAuthor": "",
                "gmvYuan": 0.0,
                "firstGmvDate": report_date,
                "lastGmvDate": report_date,
                "activeDayCount": 0,
                "sourceCutoffAt": "",
                "sourceUpdatedAt": "",
            },
        )
        current["materialName"] = str(row.get("material_name") or current["materialName"] or "")
        current["internalAuthor"] = str(row.get("internal_author") or current["internalAuthor"] or "")
        current["gmvYuan"] += gmv
        current["firstGmvDate"] = min(current["firstGmvDate"], report_date)
        current["lastGmvDate"] = max(current["lastGmvDate"], report_date)
        current["activeDayCount"] += 1
        current["sourceCutoffAt"] = str(row.get("source_cutoff_at") or current["sourceCutoffAt"] or "")
        current["sourceUpdatedAt"] = str(row.get("source_updated_at") or current["sourceUpdatedAt"] or "")

    materials = list(monthly.values())
    mapped: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for material in materials:
        signal = _normalize(f"{material['internalAuthor']} {material['materialName']}")
        exact_author = _normalize(material["internalAuthor"])
        candidates: list[tuple[dict[str, Any], str]] = []
        if exact_author:
            candidates = [
                (person, _normalize(person.get("name")))
                for person in people
                if _normalize(person.get("name")) == exact_author
            ]
        if not candidates:
            for person in people:
                matches = [
                    keyword
                    for keyword in person.get("keywords", [])
                    if _keyword_matches(signal, _normalize(keyword))
                ]
                if matches:
                    candidates.append((person, sorted(matches, key=len, reverse=True)[0]))
        if len(candidates) > 1:
            longest = max(len(keyword) for _, keyword in candidates)
            candidates = [candidate for candidate in candidates if len(candidate[1]) == longest]

        if len(candidates) == 1:
            person, keyword = candidates[0]
            mapped.append(
                {
                    **material,
                    "personName": person.get("name", ""),
                    "employeeId": person.get("employeeId", ""),
                    "department": person.get("department", ""),
                    "matchedKeyword": keyword,
                }
            )
        elif candidates:
            conflicts.append(
                {
                    **material,
                    "candidateNames": [person.get("name", "") for person, _ in candidates],
                    "matchedKeywords": [keyword for _, keyword in candidates],
                }
            )
        else:
            unmapped.append(material)

    ranking_map: dict[str, dict[str, Any]] = {}
    for material in mapped:
        name = material["personName"]
        item = ranking_map.setdefault(
            name,
            {
                "personName": name,
                "employeeId": material["employeeId"],
                "department": material["department"],
                "qianchuanGmvYuan": 0.0,
                "videoHaitunGmvYuan": 0.0,
                "videoAdqGmvYuan": 0.0,
                "videoGmvYuan": 0.0,
                "totalGmvYuan": 0.0,
                "materialKeys": set(),
                "qianchuanMaterialKeys": set(),
                "videoMaterialKeys": set(),
            },
        )
        gmv = _number(material["gmvYuan"])
        item["totalGmvYuan"] += gmv
        dedup_key = _material_dedup_key(material)
        item["materialKeys"].add(dedup_key)
        if material["channel"] == "千川":
            item["qianchuanGmvYuan"] += gmv
            item["qianchuanMaterialKeys"].add(dedup_key)
        else:
            item["videoGmvYuan"] += gmv
            item["videoMaterialKeys"].add(dedup_key)
            if material["sourcePlatform"] == "HAITUN":
                item["videoHaitunGmvYuan"] += gmv
            if material["sourcePlatform"] == "ADQ":
                item["videoAdqGmvYuan"] += gmv

    rankings = []
    for item in ranking_map.values():
        if item["totalGmvYuan"] <= 0:
            continue
        rankings.append(
            {
                "personName": item["personName"],
                "employeeId": item["employeeId"],
                "department": item["department"],
                "qianchuanGmvYuan": _round2(item["qianchuanGmvYuan"]),
                "videoHaitunGmvYuan": _round2(item["videoHaitunGmvYuan"]),
                "videoAdqGmvYuan": _round2(item["videoAdqGmvYuan"]),
                "videoGmvYuan": _round2(item["videoGmvYuan"]),
                "totalGmvYuan": _round2(item["totalGmvYuan"]),
                "materialCount": len(item["materialKeys"]),
                "qianchuanMaterialCount": len(item["qianchuanMaterialKeys"]),
                "videoMaterialCount": len(item["videoMaterialKeys"]),
            }
        )
    rankings.sort(key=lambda item: item["totalGmvYuan"], reverse=True)
    for index, item in enumerate(rankings, 1):
        item["rank"] = index

    total_source_gmv = sum(_number(item["gmvYuan"]) for item in materials)
    mapped_gmv = sum(_number(item["gmvYuan"]) for item in mapped)
    unmapped_gmv = sum(_number(item["gmvYuan"]) for item in unmapped)
    conflict_gmv = sum(_number(item["gmvYuan"]) for item in conflicts)

    source_totals: dict[str, dict[str, Any]] = {}
    for item in mapped:
        platform = item["sourcePlatform"]
        source_total = source_totals.setdefault(
            platform,
            {
                "sourcePlatform": platform,
                "channel": item["channel"],
                "gmvYuan": 0.0,
                "materialRecordCount": 0,
            },
        )
        source_total["gmvYuan"] += _number(item["gmvYuan"])
        source_total["materialRecordCount"] += 1
    for item in source_totals.values():
        item["gmvYuan"] = _round2(item["gmvYuan"])

    generated_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
    snapshot = {
        "schemaVersion": 2,
        "status": "ready" if not capped_queries else "partial",
        "generatedAt": generated_at,
        "meta": {
            "periodStart": (period_start or target_date.replace(day=1)).isoformat(),
            "periodEnd": target_date.isoformat(),
            "completeThroughDate": target_date.isoformat(),
            "qianchuanCutoffAt": target_date.isoformat(),
            "videoHaitunCutoffAt": target_date.isoformat(),
            "videoAdqCutoffDate": freshness.get("ADQ") or target_date.isoformat(),
            "schedule": f"每日{settings.personal_sales_refresh_hour:02d}:00（Asia/Shanghai）",
            "sourceLabel": "FanDo 根数据",
            "metricDefinition": {
                "qianchuan": "ROI2口径含智能优惠券成交GMV；含券字段为空时回退ROI2成交GMV",
                "videoHaitun": "海豚智投素材成交金额 transaction_amount_yuan",
                "videoAdq": "ADQ视频素材下单金额 order_amount_yuan（GMV代理口径）",
            },
            "peopleCount": len(people),
            "cappedQueries": capped_queries,
            "sourceFreshness": freshness,
        },
        "quality": {
            "totalSourceGmvYuan": _round2(total_source_gmv),
            "mappedGmvYuan": _round2(mapped_gmv),
            "unmappedGmvYuan": _round2(unmapped_gmv),
            "conflictGmvYuan": _round2(conflict_gmv),
            "mappedGmvRate": round(mapped_gmv / total_source_gmv, 4) if total_source_gmv > 0 else None,
            "sourceMaterialCount": len(materials),
            "mappedMaterialCount": len(mapped),
            "unmappedMaterialCount": len(unmapped),
            "conflictMaterialCount": len(conflicts),
        },
        "sourceTotals": sorted(source_totals.values(), key=lambda item: item["sourcePlatform"]),
        "rankings": rankings,
        "conflictMaterials": sorted(conflicts, key=lambda item: item["gmvYuan"], reverse=True)[:50],
    }
    if include_period_rows:
        snapshot["periodRows"] = [
            {
                "report_date": str(row.get("report_date") or "")[:10],
                "channel": str(row.get("channel") or ""),
                "source_platform": str(row.get("source_platform") or ""),
                "advertiser_id": str(row.get("advertiser_id") or ""),
                "material_id": str(row.get("material_id") or ""),
                "material_name": str(row.get("material_name") or ""),
                "internal_author": str(row.get("internal_author") or ""),
                "gmv_yuan": _round2(_number(row.get("gmv_yuan"))),
                "source_cutoff_at": str(row.get("source_cutoff_at") or ""),
                "source_updated_at": str(row.get("source_updated_at") or ""),
            }
            for row in daily_rows
            if _number(row.get("gmv_yuan")) > 0 and str(row.get("report_date") or "")[:10]
        ]
    return snapshot


class PersonalSalesService:
    def __init__(self) -> None:
        self.snapshot_path = Path(settings.personal_sales_snapshot_path)
        self._refresh_lock = Lock()
        self._last_attempt_at: datetime | None = None
        self._last_error = ""
        self._loaded_snapshot_path: Path | None = None
        self._loaded_snapshot_mtime_ns: int | None = None
        self._loaded_snapshot: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return bool(settings.fandow_data_mcp_token and settings.fandow_data_mcp_endpoint)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _current_snapshot(self) -> dict[str, Any]:
        path = self.snapshot_path if self.snapshot_path.exists() else SEED_SNAPSHOT_PATH
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if (
            self._loaded_snapshot is not None
            and self._loaded_snapshot_path == path
            and self._loaded_snapshot_mtime_ns == mtime_ns
        ):
            return self._loaded_snapshot
        payload = self._load_json(path)
        self._loaded_snapshot_path = path
        self._loaded_snapshot_mtime_ns = mtime_ns
        self._loaded_snapshot = payload
        return payload

    def complete_through_date(self) -> date:
        snapshot = self._current_snapshot()
        return self._parsed_date((snapshot.get("meta") or {}).get("completeThroughDate"), "快照完整日期")

    @staticmethod
    def _parsed_date(value: Any, label: str) -> date:
        try:
            return date.fromisoformat(str(value or "")[:10])
        except ValueError as error:
            raise PersonalSalesError(f"{label}不可用，请稍后重试") from error

    def get_snapshot(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, Any]:
        base = self._current_snapshot()
        snapshot = json.loads(json.dumps(base, ensure_ascii=False))
        if start_date or end_date:
            selected_start = start_date or end_date
            selected_end = end_date or start_date
            if selected_start is None or selected_end is None:
                raise PersonalSalesError("请选择查询日期")
            if selected_start > selected_end:
                raise PersonalSalesError("开始日期不能晚于结束日期")
            if (selected_end - selected_start).days > 365:
                raise PersonalSalesError("单次最多查询 366 天")
            meta = base.get("meta") or {}
            available_start = self._parsed_date(meta.get("periodStart"), "快照开始日期")
            complete_through = self._parsed_date(meta.get("completeThroughDate"), "快照完整日期")
            if selected_start < available_start:
                raise PersonalSalesError(f"当前快照仅保留 {available_start.isoformat()} 起的日明细，更早日期待回补")
            if selected_end > complete_through:
                raise SourcePendingError(
                    f"所选日期尚未完整回补，当前完整至 {complete_through.isoformat()}；不会将缺失值显示为 0"
                )
            period_rows = base.get("periodRows")
            if not isinstance(period_rows, list):
                raise SourcePendingError("按日数据正在生成，当前保留最近成功月快照，请稍后重试")
            selected_rows = [
                row
                for row in period_rows
                if selected_start.isoformat() <= str(row.get("report_date") or "")[:10] <= selected_end.isoformat()
            ]
            people = self._load_json(PEOPLE_PATH)
            snapshot = build_snapshot(
                selected_rows,
                people,
                selected_end,
                dict(meta.get("sourceFreshness") or {}),
                list(meta.get("cappedQueries") or []),
                period_start=selected_start,
                include_period_rows=False,
            )
            snapshot["generatedAt"] = base.get("generatedAt")
            snapshot["meta"]["completeThroughDate"] = complete_through.isoformat()
        snapshot.pop("periodRows", None)
        snapshot["automation"] = self.status(base)
        return snapshot

    def expected_target_date(self, now: datetime | None = None) -> date:
        current = now.astimezone(SHANGHAI_TZ) if now else datetime.now(SHANGHAI_TZ)
        lag_days = 1 if current.hour >= settings.personal_sales_refresh_hour else 2
        return current.date() - timedelta(days=lag_days)

    def status(self, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        if snapshot is None:
            snapshot = self._current_snapshot()
        complete_through = str((snapshot.get("meta") or {}).get("completeThroughDate") or "")
        target = self.expected_target_date()
        return {
            "configured": self.configured,
            "schedule": f"每日{settings.personal_sales_refresh_hour:02d}:00",
            "expectedCompleteThroughDate": target.isoformat(),
            "isFresh": bool(complete_through and complete_through >= target.isoformat()),
            "lastSuccessAt": snapshot.get("generatedAt"),
            "lastAttemptAt": self._last_attempt_at.isoformat(timespec="seconds") if self._last_attempt_at else None,
            "lastError": self._last_error,
            "preservesLastSuccessOnFailure": True,
        }

    def _write_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(self.snapshot_path)

    def refresh(self, target_date: date | None = None) -> dict[str, Any]:
        if not self.configured:
            raise PersonalSalesError("FanDo 根数据连接尚未配置，已保留上次成功快照")
        target = target_date or self.expected_target_date()
        start = target.replace(day=1)
        client = FandowDataMcpClient(
            settings.fandow_data_mcp_endpoint,
            settings.fandow_data_mcp_token,
            settings.fandow_data_mcp_timeout_seconds,
        )
        client.initialize()
        freshness_rows, freshness_capped = client.run_sql("根数据新鲜度", _freshness_sql(target), 100)
        if freshness_capped:
            raise PersonalSalesError("根数据新鲜度查询异常截断，已保留上次成功快照")
        freshness = {
            str(row.get("source_name") or ""): str(row.get("data_date") or "")[:10]
            for row in freshness_rows
        }
        qianchuan_date = max(
            freshness.get("QIANCHUAN_STANDARD", ""),
            freshness.get("QIANCHUAN_CHENGFANG", ""),
        )
        pending = []
        if qianchuan_date < target.isoformat():
            pending.append("千川")
        if freshness.get("HAITUN", "") < target.isoformat():
            pending.append("视频号海豚")
        if freshness.get("ADQ", "") < target.isoformat():
            pending.append("视频号ADQ")
        if pending:
            raise SourcePendingError(f"{'、'.join(pending)}尚未完整到达{target.isoformat()}，已保留上次成功快照")

        daily_rows: list[dict[str, Any]] = []
        capped_queries: list[str] = []
        for day in _date_range(start, target):
            queries = [
                (f"千川标准 {day.isoformat()}", _standard_qianchuan_sql(day)),
                (f"千川乘方 {day.isoformat()}", _chengfang_qianchuan_sql(day)),
                (f"视频号海豚 {day.isoformat()}", _haitun_sql(day)),
                (f"视频号ADQ {day.isoformat()}", _adq_sql(day)),
            ]
            for label, sql in queries:
                rows, capped = client.run_sql(label, sql)
                daily_rows.extend(rows)
                if capped:
                    capped_queries.append(label)
        if capped_queries:
            raise PersonalSalesError(f"根数据查询达到行数上限：{', '.join(capped_queries)}；已保留上次成功快照")

        people = self._load_json(PEOPLE_PATH)
        snapshot = build_snapshot(daily_rows, people, target, freshness, capped_queries)
        self._write_snapshot(snapshot)
        self._last_error = ""
        return snapshot

    def refresh_if_due(self, now: datetime | None = None) -> dict[str, Any]:
        current = now.astimezone(SHANGHAI_TZ) if now else datetime.now(SHANGHAI_TZ)
        self._last_attempt_at = current
        if not self.configured:
            self._last_error = "FanDo 根数据连接尚未配置，当前继续展示上次成功快照"
            return self.get_snapshot()
        if not self._refresh_lock.acquire(blocking=False):
            return self.get_snapshot()
        try:
            snapshot = self._current_snapshot()
            complete_through = str((snapshot.get("meta") or {}).get("completeThroughDate") or "")
            target = self.expected_target_date(current)
            if complete_through >= target.isoformat() and isinstance(snapshot.get("periodRows"), list):
                return self.get_snapshot()
            return self.refresh(target)
        except Exception as error:
            self._last_error = str(error)[:500]
            return self.get_snapshot()
        finally:
            self._refresh_lock.release()


personal_sales_service = PersonalSalesService()
