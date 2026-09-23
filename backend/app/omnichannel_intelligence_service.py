from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .config import settings
from .personal_sales_service import FandowDataMcpClient, PersonalSalesError, SHANGHAI_TZ


class OmnichannelIntelligenceError(PersonalSalesError):
    pass


DOUYIN_ORDER_TABLE = "d_root_project_mid_db_brandmarketing.platform_order_item_api_field"
WECHAT_ORDER_TABLE = "d_root_project_order_db.platform_order_item_api_field"
QIANCHUAN_ACCOUNT_TABLE = "d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost"
QIANCHUAN_PLAN_TABLE = "d_root_project_mid_db_brandmarketing.qianchuan_ad_plan_hourly_delivery_cost_detail"
QIANCHUAN_CHENGFANG_PLAN_TABLE = "d_root_project_mid_db_brandmarketing.qianchuan_chengfang_ad_plan_hourly_delivery_cost_detail"
WECHAT_ROOT_DB = "d_root_project_marketing_db"
WECHAT_SHOP_PROMOTION_TABLE = f"{WECHAT_ROOT_DB}.wechat_shop_promotion_product_daily_performance"
HAITUN_LIVE_TABLE = f"{WECHAT_ROOT_DB}.haitun_zhitou_live_account_hourly_summary"
HAITUN_SHORT_TABLE = f"{WECHAT_ROOT_DB}.haitun_zhitou_short_video_account_hourly_summary"
WIS_WECHAT_SHOP_ID = 1179
WIS_HAITUN_ACCOUNT_ID = "133536"


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_number(value: Any) -> float | None:
    return None if value is None or value == "" else _number(value)


def _truthy(value: Any) -> bool:
    return _number(value) != 0


def _date_text(value: Any) -> str:
    return str(value or "")[:10]


def _hourly_gsv_sql(start: date, end: date) -> str:
    return f"""SELECT 'douyin' AS channel_key,
  DATE(o.pay_time) AS pay_date,
  HOUR(o.pay_time) AS hour_no,
  ROUND(SUM(CASE WHEN o.order_status <> '4' THEN o.order_pay_amount ELSE 0 END) / 100, 2) AS gsv_yuan,
  MAX(o.updated_at) AS source_updated_at
FROM {DOUYIN_ORDER_TABLE} o FORCE INDEX (idx_shop_pay_time)
WHERE o.shop_id IN (1099, 1100)
  AND o.pay_time >= TIMESTAMP('{start.isoformat()} 00:00:00')
  AND o.pay_time < DATE_ADD(DATE('{end.isoformat()}'), INTERVAL 1 DAY)
GROUP BY DATE(o.pay_time), HOUR(o.pay_time)
UNION ALL
SELECT 'wechat' AS channel_key,
  DATE(o.pay_time) AS pay_date,
  HOUR(o.pay_time) AS hour_no,
  ROUND(SUM(CASE WHEN o.order_status NOT IN ('200', '250') THEN o.order_pay_amount ELSE 0 END) / 100, 2) AS gsv_yuan,
  MAX(o.updated_at) AS source_updated_at
FROM {WECHAT_ORDER_TABLE} o FORCE INDEX (idx_shop_pay_time)
WHERE o.shop_id = {WIS_WECHAT_SHOP_ID}
  AND o.pay_time >= TIMESTAMP('{start.isoformat()} 00:00:00')
  AND o.pay_time < DATE_ADD(DATE('{end.isoformat()}'), INTERVAL 1 DAY)
GROUP BY DATE(o.pay_time), HOUR(o.pay_time)
ORDER BY channel_key, pay_date, hour_no"""


def _douyin_hourly_spend_sql(day: date) -> str:
    value = day.isoformat()
    wis_accounts = f"""SELECT DISTINCT advertiser_id
FROM {QIANCHUAN_ACCOUNT_TABLE} FORCE INDEX (idx_qc_account_hourly_time)
WHERE stat_time >= DATE_SUB(CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', '+08:00'), INTERVAL 2 DAY)"""
    return f"""WITH plan_rows AS (
  SELECT 'standard' AS source_name,
    q.id, q.report_date, q.stat_time, q.advertiser_id, q.marketing_goal_code,
    q.ad_id, q.time_granularity, q.stat_cost, q.plan_total_cost_yuan, q.observed_at
  FROM {QIANCHUAN_PLAN_TABLE} q FORCE INDEX (idx_qc_plan_material_daily_ad)
  WHERE q.report_date = DATE('{value}')
    AND q.record_grain = 'PLAN'
    AND q.advertiser_id IN ({wis_accounts})
  UNION ALL
  SELECT 'chengfang' AS source_name,
    q.id, q.report_date, q.stat_time, q.advertiser_id, q.marketing_goal_code,
    q.ad_id, q.time_granularity, q.stat_cost, q.plan_total_cost_yuan, q.observed_at
  FROM {QIANCHUAN_CHENGFANG_PLAN_TABLE} q FORCE INDEX (idx_qc_plan_material_daily_ad)
  WHERE q.report_date = DATE('{value}')
    AND q.record_grain = 'PLAN'
), ranked AS (
  SELECT q.*,
    COALESCE(q.stat_cost, q.plan_total_cost_yuan, 0) AS cost_yuan,
    ROW_NUMBER() OVER (
      PARTITION BY q.source_name, q.report_date, q.stat_time, q.advertiser_id,
        q.marketing_goal_code, q.ad_id, q.time_granularity
      ORDER BY q.observed_at DESC, q.id DESC
    ) AS snapshot_rank
  FROM plan_rows q
), deduplicated AS (
  SELECT * FROM ranked WHERE snapshot_rank = 1
), first_five_minute AS (
  SELECT source_name, report_date, advertiser_id, marketing_goal_code, ad_id,
    MIN(stat_time) AS first_stat_time
  FROM deduplicated
  WHERE time_granularity = 'FIVE_MINUTE'
  GROUP BY source_name, report_date, advertiser_id, marketing_goal_code, ad_id
), hour_before_snapshot AS (
  SELECT h.source_name, h.report_date, h.advertiser_id, h.marketing_goal_code, h.ad_id,
    SUM(h.cost_yuan) AS cost_yuan
  FROM deduplicated h
  JOIN first_five_minute f
    ON f.source_name = h.source_name
   AND f.report_date = h.report_date
   AND f.advertiser_id = h.advertiser_id
   AND f.marketing_goal_code = h.marketing_goal_code
   AND f.ad_id = h.ad_id
  WHERE h.time_granularity = 'HOUR' AND h.stat_time < f.first_stat_time
  GROUP BY h.source_name, h.report_date, h.advertiser_id, h.marketing_goal_code, h.ad_id
), hour_increments AS (
  SELECT h.report_date, h.stat_time, h.cost_yuan, h.observed_at
  FROM deduplicated h
  LEFT JOIN first_five_minute f
    ON f.source_name = h.source_name
   AND f.report_date = h.report_date
   AND f.advertiser_id = h.advertiser_id
   AND f.marketing_goal_code = h.marketing_goal_code
   AND f.ad_id = h.ad_id
  WHERE h.time_granularity = 'HOUR'
    AND (f.first_stat_time IS NULL OR h.stat_time < f.first_stat_time)
), five_minute_sequence AS (
  SELECT d.*,
    LAG(d.cost_yuan) OVER (
      PARTITION BY d.source_name, d.report_date, d.advertiser_id, d.marketing_goal_code, d.ad_id
      ORDER BY d.stat_time
    ) AS previous_cost_yuan
  FROM deduplicated d
  WHERE d.time_granularity = 'FIVE_MINUTE'
), five_minute_increments AS (
  SELECT f.report_date, f.stat_time,
    CASE WHEN f.previous_cost_yuan IS NULL
      THEN f.cost_yuan - COALESCE(h.cost_yuan, 0)
      ELSE f.cost_yuan - f.previous_cost_yuan END AS cost_yuan,
    f.observed_at
  FROM five_minute_sequence f
  LEFT JOIN hour_before_snapshot h
    ON h.source_name = f.source_name
   AND h.report_date = f.report_date
   AND h.advertiser_id = f.advertiser_id
   AND h.marketing_goal_code = f.marketing_goal_code
   AND h.ad_id = f.ad_id
), increments AS (
  SELECT * FROM hour_increments
  UNION ALL
  SELECT * FROM five_minute_increments
), bucketed AS (
  SELECT CASE WHEN stat_time >= DATE_ADD(report_date, INTERVAL 1 DAY)
      THEN 23 ELSE HOUR(stat_time) END AS hour_no,
    cost_yuan, observed_at
  FROM increments
)
SELECT '{value}' AS report_date, hour_no,
  ROUND(SUM(cost_yuan), 2) AS spend_yuan,
  MAX(observed_at) AS source_updated_at
FROM bucketed
GROUP BY hour_no
ORDER BY hour_no"""


def _wechat_daily_spend_sql(start: date, end: date) -> str:
    start_value = start.isoformat()
    end_value = end.isoformat()
    return f"""WITH adq_completed AS (
  SELECT report_date AS stat_date, SUM(cost_yuan) AS adq_spend_yuan,
    SUM(order_net_amount_yuan) AS attributed_net_yuan, MAX(updated_at) AS updated_at
  FROM {WECHAT_ROOT_DB}.adq_account_daily_databoard
  WHERE report_date BETWEEN DATE('{start_value}') AND DATE('{end_value}')
    AND report_date < CURRENT_DATE AND business_alias LIKE '%WIS%'
  GROUP BY report_date
), adq_today AS (
  SELECT report_date AS stat_date, SUM(cost_yuan) AS adq_spend_yuan,
    SUM(order_net_amount_yuan) AS attributed_net_yuan, MAX(updated_at) AS updated_at
  FROM {WECHAT_ROOT_DB}.adq_account_hourly_databoard
  WHERE report_date BETWEEN DATE('{start_value}') AND DATE('{end_value}')
    AND report_date = CURRENT_DATE AND business_alias LIKE '%WIS%'
  GROUP BY report_date
), adq AS (
  SELECT * FROM adq_completed UNION ALL SELECT * FROM adq_today
), shop_promotion AS (
  SELECT stat_date, SUM(cost_yuan) AS shop_promotion_spend_yuan, MAX(updated_at) AS updated_at
  FROM {WECHAT_SHOP_PROMOTION_TABLE}
  WHERE shop_id = {WIS_WECHAT_SHOP_ID}
    AND stat_date BETWEEN DATE('{start_value}') AND DATE('{end_value}')
  GROUP BY stat_date
), creator_commission AS (
  SELECT DATE(pay_time) AS stat_date,
    SUM(COALESCE(promotion_commission_amount, 0)) / 100.0 AS creator_commission_spend_yuan,
    MAX(updated_at) AS updated_at
  FROM {WECHAT_ORDER_TABLE} FORCE INDEX (idx_shop_pay_time)
  WHERE shop_id = {WIS_WECHAT_SHOP_ID}
    AND pay_time >= TIMESTAMP('{start_value} 00:00:00')
    AND pay_time < DATE_ADD(DATE('{end_value}'), INTERVAL 1 DAY)
    AND order_status NOT IN ('200', '250')
  GROUP BY DATE(pay_time)
), haitun_live_ranked AS (
  SELECT stat_date, platform_account_id, live_scene_code, cumulative_cost_yuan, updated_at,
    ROW_NUMBER() OVER (
      PARTITION BY stat_date, platform_account_id, live_scene_code
      ORDER BY collection_time DESC, updated_at DESC, id DESC
    ) AS rn
  FROM {HAITUN_LIVE_TABLE}
  WHERE stat_date BETWEEN DATE('{start_value}') AND DATE('{end_value}')
    AND platform_account_id = '{WIS_HAITUN_ACCOUNT_ID}'
), haitun_live AS (
  SELECT stat_date, SUM(cumulative_cost_yuan) AS live_spend_yuan, MAX(updated_at) AS updated_at
  FROM haitun_live_ranked WHERE rn = 1 GROUP BY stat_date
), haitun_short_ranked AS (
  SELECT stat_date, platform_account_id, filter_type, filter_id, cumulative_cost_yuan, updated_at,
    ROW_NUMBER() OVER (
      PARTITION BY stat_date, platform_account_id, filter_type, filter_id
      ORDER BY collection_time DESC, updated_at DESC, id DESC
    ) AS rn
  FROM {HAITUN_SHORT_TABLE}
  WHERE stat_date BETWEEN DATE('{start_value}') AND DATE('{end_value}')
    AND platform_account_id = '{WIS_HAITUN_ACCOUNT_ID}' AND filter_type = 'ALL'
), haitun_short AS (
  SELECT stat_date, SUM(cumulative_cost_yuan) AS short_video_spend_yuan, MAX(updated_at) AS updated_at
  FROM haitun_short_ranked WHERE rn = 1 GROUP BY stat_date
), haitun_dates AS (
  SELECT stat_date FROM haitun_live UNION SELECT stat_date FROM haitun_short
), wechat_bean AS (
  SELECT d.stat_date,
    CASE WHEN l.stat_date IS NOT NULL AND s.stat_date IS NOT NULL
      THEN l.live_spend_yuan + s.short_video_spend_yuan ELSE NULL END AS wechat_bean_spend_yuan,
    (l.stat_date IS NOT NULL AND s.stat_date IS NOT NULL) AS has_wechat_bean_data,
    NULLIF(GREATEST(COALESCE(l.updated_at, '1970-01-01 00:00:00'),
      COALESCE(s.updated_at, '1970-01-01 00:00:00')), '1970-01-01 00:00:00') AS updated_at
  FROM haitun_dates d
  LEFT JOIN haitun_live l ON l.stat_date = d.stat_date
  LEFT JOIN haitun_short s ON s.stat_date = d.stat_date
), cost_dates AS (
  SELECT stat_date FROM adq UNION SELECT stat_date FROM shop_promotion
  UNION SELECT stat_date FROM creator_commission UNION SELECT stat_date FROM wechat_bean
)
SELECT d.stat_date, a.adq_spend_yuan, p.shop_promotion_spend_yuan,
  c.creator_commission_spend_yuan, w.wechat_bean_spend_yuan,
  COALESCE(a.adq_spend_yuan, 0) + COALESCE(p.shop_promotion_spend_yuan, 0)
    + COALESCE(c.creator_commission_spend_yuan, 0) + COALESCE(w.wechat_bean_spend_yuan, 0) AS spend_yuan,
  a.stat_date IS NOT NULL AS has_adq_data,
  p.stat_date IS NOT NULL AS has_shop_promotion_data,
  c.stat_date IS NOT NULL AS has_commission_data,
  COALESCE(w.has_wechat_bean_data, 0) AS has_wechat_bean_data,
  NULLIF(GREATEST(COALESCE(a.updated_at, '1970-01-01 00:00:00'),
    COALESCE(p.updated_at, '1970-01-01 00:00:00'),
    COALESCE(c.updated_at, '1970-01-01 00:00:00'),
    COALESCE(w.updated_at, '1970-01-01 00:00:00')), '1970-01-01 00:00:00') AS source_updated_at
FROM cost_dates d
LEFT JOIN adq a ON a.stat_date = d.stat_date
LEFT JOIN shop_promotion p ON p.stat_date = d.stat_date
LEFT JOIN creator_commission c ON c.stat_date = d.stat_date
LEFT JOIN wechat_bean w ON w.stat_date = d.stat_date
ORDER BY d.stat_date"""


def _douyin_spend_cutoff_sql(
    cutoff_key: str,
    report_date: date,
    window_start: datetime,
    window_end: datetime,
) -> str:
    if cutoff_key not in {"today_total", "today_comparison", "yesterday_comparison", "yesterday_total"}:
        raise ValueError("无效投放截止点")
    day = report_date.isoformat()
    start = window_start.strftime("%Y-%m-%d %H:%M:%S")
    end = window_end.strftime("%Y-%m-%d %H:%M:%S")
    return f"""WITH standard_latest AS (
  SELECT m.advertiser_id, MAX(m.stat_time) AS stat_time
  FROM d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report m FORCE INDEX (uk_qc_material_hourly)
  WHERE m.report_date = DATE('{day}')
    AND m.stat_time BETWEEN TIMESTAMP('{start}') AND TIMESTAMP('{end}')
  GROUP BY m.advertiser_id
), standard_totals AS (
  SELECT ROUND(SUM(COALESCE(m.stat_cost_for_roi2, 0)), 2) AS spend_yuan,
    MAX(m.updated_at) AS source_updated_at
  FROM standard_latest l
  JOIN d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report m FORCE INDEX (uk_qc_material_hourly)
    ON m.report_date = DATE('{day}')
   AND m.advertiser_id = l.advertiser_id
   AND m.stat_time = l.stat_time
  WHERE COALESCE(NULLIF(m.platform_material_type, ''), 'VIDEO') = 'VIDEO'
), chengfang_latest AS (
  SELECT m.advertiser_id, MAX(m.stat_time) AS stat_time
  FROM d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report m FORCE INDEX (uk_qc_material_hourly)
  WHERE m.report_date = DATE('{day}')
    AND m.stat_time BETWEEN TIMESTAMP('{start}') AND TIMESTAMP('{end}')
  GROUP BY m.advertiser_id
), chengfang_totals AS (
  SELECT ROUND(SUM(COALESCE(m.stat_cost_for_roi2, 0)), 2) AS spend_yuan,
    MAX(m.updated_at) AS source_updated_at
  FROM chengfang_latest l
  JOIN d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report m FORCE INDEX (uk_qc_material_hourly)
    ON m.report_date = DATE('{day}')
   AND m.advertiser_id = l.advertiser_id
   AND m.stat_time = l.stat_time
  WHERE COALESCE(NULLIF(m.platform_material_type, ''), 'VIDEO') = 'VIDEO'
), combined AS (
  SELECT * FROM standard_totals UNION ALL SELECT * FROM chengfang_totals
)
SELECT '{cutoff_key}' AS cutoff_key,
  ROUND(SUM(spend_yuan), 2) AS spend_yuan,
  MAX(source_updated_at) AS source_updated_at
FROM combined"""


def _sum_spend_through(rows: list[dict[str, Any]], target: str, through_hour: int | None) -> float | None:
    if through_hour is None:
        return None
    matches = [row for row in rows if row["date"] == target and row["hour"] <= through_hour]
    return round(sum(row["spendYuan"] for row in matches), 2) if matches else None


def _sum_spend_for_date(rows: list[dict[str, Any]], target: str) -> float | None:
    matches = [row for row in rows if row["date"] == target]
    return round(sum(row["spendYuan"] for row in matches), 2) if matches else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * 100


def _build_series(
    key: str,
    label: str,
    today: str,
    yesterday: str,
    rows: list[dict[str, Any]],
    spend: dict[str, Any],
    complete_hour_limit: int | None,
) -> dict[str, Any]:
    today_rows = [row for row in rows if row["date"] == today]
    yesterday_rows = [row for row in rows if row["date"] == yesterday]
    data_through_hour = max((row["hour"] for row in today_rows), default=None)
    comparison_through_hour = (
        None if data_through_hour is None or complete_hour_limit is None
        else min(data_through_hour, complete_hour_limit)
    )
    today_by_hour = {row["hour"]: row["gsvYuan"] for row in today_rows}
    yesterday_by_hour = {row["hour"]: row["gsvYuan"] for row in yesterday_rows}
    today_cumulative = 0.0
    yesterday_cumulative = 0.0
    points = []
    for hour in range(24):
        today_hourly = today_by_hour.get(hour, 0.0) if comparison_through_hour is not None and hour <= comparison_through_hour else None
        yesterday_hourly = yesterday_by_hour.get(hour, 0.0) if yesterday_rows else None
        if today_hourly is not None:
            today_cumulative += today_hourly
        if yesterday_hourly is not None:
            yesterday_cumulative += yesterday_hourly
        points.append({
            "hour": hour,
            "hourLabel": f"{hour:02d}:00",
            "todayHourlyYuan": None if today_hourly is None else round(today_hourly, 2),
            "yesterdayHourlyYuan": None if yesterday_hourly is None else round(yesterday_hourly, 2),
            "todayCumulativeYuan": None if today_hourly is None else round(today_cumulative, 2),
            "yesterdayCumulativeYuan": None if yesterday_hourly is None else round(yesterday_cumulative, 2),
        })
    today_total = round(sum(row["gsvYuan"] for row in today_rows), 2) if data_through_hour is not None else None
    today_comparison = points[comparison_through_hour]["todayCumulativeYuan"] if comparison_through_hour is not None else None
    yesterday_same_time = points[comparison_through_hour]["yesterdayCumulativeYuan"] if comparison_through_hour is not None and yesterday_rows else None
    yesterday_total = points[-1]["yesterdayCumulativeYuan"] if yesterday_rows else None
    comparison_pct = (
        (today_comparison - yesterday_same_time) / yesterday_same_time * 100
        if today_comparison is not None and yesterday_same_time is not None and yesterday_same_time > 0
        else None
    )
    comparison_today_spend = spend.get("comparisonTodaySpendYuan")
    comparison_today_spend_ratio = _ratio(comparison_today_spend, today_comparison)
    yesterday_same_time_spend_ratio = _ratio(spend.get("yesterdaySameTimeSpendYuan"), yesterday_same_time)
    spend_ratio_comparison = (
        (comparison_today_spend_ratio - yesterday_same_time_spend_ratio) / yesterday_same_time_spend_ratio * 100
        if comparison_today_spend_ratio is not None
        and yesterday_same_time_spend_ratio is not None
        and yesterday_same_time_spend_ratio > 0
        else None
    )
    source_updated_at = max((str(row.get("sourceUpdatedAt") or "") for row in rows), default="") or None
    reason = None
    if not today_rows:
        reason = "今日根数据尚未返回"
    elif not yesterday_rows:
        reason = "昨日小时根数据尚未返回"
    return {
        "key": key,
        "label": label,
        "metric": "GSV",
        "todayDate": today,
        "yesterdayDate": yesterday,
        "dataThroughHour": data_through_hour,
        "comparisonThroughHour": comparison_through_hour,
        "todayTotalYuan": today_total,
        "todayComparisonYuan": today_comparison,
        "yesterdaySameTimeYuan": yesterday_same_time,
        "yesterdayTotalYuan": yesterday_total,
        "comparisonPct": comparison_pct,
        "todaySpendYuan": spend.get("todaySpendYuan"),
        "yesterdaySameTimeSpendYuan": spend.get("yesterdaySameTimeSpendYuan"),
        "yesterdayTotalSpendYuan": spend.get("yesterdayTotalSpendYuan"),
        "todaySpendRatioPct": _ratio(spend.get("todaySpendYuan"), today_total),
        "yesterdaySameTimeSpendRatioPct": yesterday_same_time_spend_ratio,
        "yesterdayTotalSpendRatioPct": _ratio(spend.get("yesterdayTotalSpendYuan"), yesterday_total),
        "spendRatioComparisonPct": spend_ratio_comparison,
        "spendCoverage": spend.get("coverage", "missing"),
        "yesterdaySpendCoverage": spend.get("yesterdayCoverage", spend.get("coverage", "missing")),
        "spendBreakdown": spend.get("breakdown", []),
        "sourceUpdatedAt": source_updated_at,
        "state": "partial" if rows else "missing",
        "unavailableReason": reason,
        "points": points,
    }


def build_realtime_snapshot(
    hourly_gsv_rows: list[dict[str, Any]],
    douyin_spend_rows: list[dict[str, Any]],
    wechat_spend_rows: list[dict[str, Any]],
    today: date,
    current_hour: int,
) -> dict[str, Any]:
    yesterday = today - timedelta(days=1)
    today_text = today.isoformat()
    yesterday_text = yesterday.isoformat()
    complete_hour_limit = None if current_hour == 0 else current_hour - 1
    gsv_by_channel: dict[str, list[dict[str, Any]]] = {"douyin": [], "wechat": []}
    for row in hourly_gsv_rows:
        key = str(row.get("channel_key") or "")
        hour = int(_number(row.get("hour_no")))
        if key not in gsv_by_channel or hour < 0 or hour > 23:
            continue
        gsv_by_channel[key].append({
            "date": _date_text(row.get("pay_date")),
            "hour": hour,
            "gsvYuan": _number(row.get("gsv_yuan")),
            "sourceUpdatedAt": str(row.get("source_updated_at") or "") or None,
        })
    cutoff_spend = {
        str(row.get("cutoff_key")): _optional_number(row.get("spend_yuan"))
        for row in douyin_spend_rows
        if row.get("cutoff_key")
    }
    normalized_douyin_spend = []
    if not cutoff_spend:
        for row in douyin_spend_rows:
            hour = int(_number(row.get("hour_no")))
            if 0 <= hour <= 23:
                normalized_douyin_spend.append({
                    "date": _date_text(row.get("report_date")),
                    "hour": hour,
                    "spendYuan": _number(row.get("spend_yuan")),
                })
    douyin_data_through = max((row["hour"] for row in gsv_by_channel["douyin"] if row["date"] == today_text), default=None)
    douyin_today_spend = cutoff_spend.get("today_total") if cutoff_spend else _sum_spend_through(normalized_douyin_spend, today_text, douyin_data_through)
    douyin_comparison_spend = cutoff_spend.get("today_comparison") if cutoff_spend else _sum_spend_through(normalized_douyin_spend, today_text, complete_hour_limit)
    douyin_yesterday_same_time_spend = cutoff_spend.get("yesterday_comparison") if cutoff_spend else _sum_spend_through(normalized_douyin_spend, yesterday_text, complete_hour_limit)
    douyin_yesterday_spend = cutoff_spend.get("yesterday_total") if cutoff_spend else _sum_spend_for_date(normalized_douyin_spend, yesterday_text)
    douyin_spend = {
        "todaySpendYuan": douyin_today_spend,
        "comparisonTodaySpendYuan": douyin_comparison_spend,
        "yesterdaySameTimeSpendYuan": douyin_yesterday_same_time_spend,
        "yesterdayTotalSpendYuan": douyin_yesterday_spend,
        # The low-latency cutoff query reads the latest material snapshots.  It is
        # intentionally not presented as the complete plan-level Qianchuan cost.
        "coverage": "partial" if cutoff_spend and douyin_today_spend is not None else "complete" if douyin_today_spend is not None else "missing",
        "yesterdayCoverage": "partial" if cutoff_spend and douyin_yesterday_spend is not None else "complete" if douyin_yesterday_spend is not None else "missing",
        "breakdown": [{"key": "qianchuan-visible-materials", "label": "千川可见素材", "valueYuan": douyin_today_spend}],
    }
    wechat_by_date = {_date_text(row.get("stat_date")): row for row in wechat_spend_rows}

    def wechat_day_spend(target: str) -> dict[str, Any]:
        row = wechat_by_date.get(target)
        if not row:
            return {"total": None, "coverage": "missing", "breakdown": []}
        flags = {
            "adq": _truthy(row.get("has_adq_data")),
            "shop": _truthy(row.get("has_shop_promotion_data")),
            "commission": _truthy(row.get("has_commission_data")),
            "haitun": _truthy(row.get("has_wechat_bean_data")),
        }
        return {
            "total": _number(row.get("spend_yuan")) if any(flags.values()) else None,
            "coverage": "complete" if all(flags.values()) else "partial" if any(flags.values()) else "missing",
            "breakdown": [
                {"key": "adq", "label": "ADQ", "valueYuan": _optional_number(row.get("adq_spend_yuan")) if flags["adq"] else None},
                {"key": "haitun", "label": "海豚智投", "valueYuan": _optional_number(row.get("wechat_bean_spend_yuan")) if flags["haitun"] else None},
                {"key": "shop", "label": "店铺推广", "valueYuan": _optional_number(row.get("shop_promotion_spend_yuan")) if flags["shop"] else None},
                {"key": "commission", "label": "达人佣金", "valueYuan": _optional_number(row.get("creator_commission_spend_yuan")) if flags["commission"] else None},
            ],
        }

    wechat_today = wechat_day_spend(today_text)
    wechat_yesterday = wechat_day_spend(yesterday_text)
    wechat_spend = {
        "todaySpendYuan": wechat_today["total"],
        "comparisonTodaySpendYuan": None,
        "yesterdaySameTimeSpendYuan": None,
        "yesterdayTotalSpendYuan": wechat_yesterday["total"],
        "coverage": wechat_today["coverage"],
        "yesterdayCoverage": wechat_yesterday["coverage"],
        "breakdown": wechat_today["breakdown"],
    }
    channels = [
        _build_series("douyin", "抖音", today_text, yesterday_text, gsv_by_channel["douyin"], douyin_spend, complete_hour_limit),
        _build_series("wechat", "视频号", today_text, yesterday_text, gsv_by_channel["wechat"], wechat_spend, complete_hour_limit),
    ]
    combined_total = (
        round(sum(channel["todayTotalYuan"] for channel in channels), 2)
        if all(channel["todayTotalYuan"] is not None for channel in channels)
        else None
    )
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "status": "ready" if all(channel["unavailableReason"] is None for channel in channels) else "partial",
        "summary": {
            "departmentTodayGsvYuan": combined_total,
            "includedChannels": ["douyin", "wechat"],
        },
        "definitions": {
            "departmentPerformance": "品牌营销部业绩 = 抖店有效 GSV + 视频号有效 GSV；两个渠道独立展示后再合计。",
            "douyinGsv": "抖店支付订单中排除退款/关闭状态后的有效成交额。",
            "wechatGsv": "视频号支付订单中排除退款和关闭状态后的有效成交额。",
            "qianchuanAttribution": "千川归因 GMV 是投放归因口径，只用于素材与投放分析，不重复计入店铺有效 GSV。",
            "spendCoverage": "抖店费用当前来自千川可见素材快照，属于部分覆盖；视频号费用按已返回的 ADQ、海豚智投、店铺推广和达人佣金分别标注覆盖状态。",
            "comparison": "同比与曲线只比较已经完整结束的小时；当前小时仅计入实时累计卡，不参与同比。",
        },
        "channels": channels,
    }


class OmnichannelIntelligenceService:
    def __init__(
        self,
        snapshot_path: str | Path | None = None,
        client_factory: Callable[..., FandowDataMcpClient] = FandowDataMcpClient,
        cache_seconds: int = 300,
    ) -> None:
        default_path = Path(settings.business_intelligence_snapshot_path).with_name("omnichannel_realtime_snapshot.json")
        self.snapshot_path = Path(snapshot_path or default_path)
        self.client_factory = client_factory
        self.cache_seconds = max(60, cache_seconds)
        self._lock = Lock()
        self._last_error = ""

    @property
    def configured(self) -> bool:
        return bool(settings.fandow_data_mcp_token and settings.fandow_data_mcp_endpoint)

    def _load(self) -> dict[str, Any] | None:
        try:
            with self.snapshot_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write(self, snapshot: dict[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(self.snapshot_path)

    def _fresh(self, snapshot: dict[str, Any] | None, now: datetime) -> bool:
        if not snapshot:
            return False
        generated = str(snapshot.get("generatedAt") or "")
        try:
            generated_at = datetime.fromisoformat(generated)
        except ValueError:
            return False
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=SHANGHAI_TZ)
        channels = snapshot.get("channels") or []
        return (
            bool(channels)
            and channels[0].get("todayDate") == now.date().isoformat()
            and (now - generated_at.astimezone(SHANGHAI_TZ)).total_seconds() < self.cache_seconds
        )

    def _run_sql(self, label: str, sql: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        client = self.client_factory(
            settings.fandow_data_mcp_endpoint,
            settings.fandow_data_mcp_token,
            settings.fandow_data_mcp_timeout_seconds,
        )
        client.initialize()
        return client.run_sql(label, sql, limit)

    def refresh(self, now: datetime | None = None) -> dict[str, Any]:
        if not self.configured:
            raise OmnichannelIntelligenceError("FanDo 根数据连接尚未配置")
        current = now.astimezone(SHANGHAI_TZ) if now else datetime.now(SHANGHAI_TZ)
        today = current.date()
        yesterday = today - timedelta(days=1)
        complete_hour = None if current.hour == 0 else current.hour - 1
        cutoff_specs: list[tuple[str, date, datetime, datetime]] = []
        today_total_end = current
        cutoff_specs.append(("today_total", today, today_total_end - timedelta(minutes=30), today_total_end))
        yesterday_total_end = current.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_specs.append((
            "yesterday_total", yesterday,
            yesterday_total_end - timedelta(minutes=30), yesterday_total_end,
        ))
        if complete_hour is not None:
            today_comparison_end = current.replace(hour=complete_hour, minute=59, second=59, microsecond=0)
            yesterday_comparison_end = today_comparison_end - timedelta(days=1)
            cutoff_specs.extend([
                (
                    "today_comparison", today,
                    today_comparison_end - timedelta(minutes=30), today_comparison_end,
                ),
                (
                    "yesterday_comparison", yesterday,
                    yesterday_comparison_end - timedelta(minutes=30), yesterday_comparison_end,
                ),
            ])

        jobs: dict[str, tuple[str, str, int]] = {
            "gsv": ("抖店与视频号实时GSV", _hourly_gsv_sql(yesterday, today), 200),
            "wechat_spend": ("视频号渠道费用", _wechat_daily_spend_sql(yesterday, today), 20),
        }
        for key, day, window_start, window_end in cutoff_specs:
            jobs[f"douyin:{key}"] = (
                f"抖音投放截止点 {key}",
                _douyin_spend_cutoff_sql(key, day, window_start, window_end),
                5,
            )

        results: dict[str, list[dict[str, Any]]] = {}
        failures: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(5, len(jobs))) as executor:
            pending = {
                executor.submit(self._run_sql, label, sql, limit): (key, label, limit)
                for key, (label, sql, limit) in jobs.items()
            }
            for future in as_completed(pending):
                key, label, limit = pending[future]
                try:
                    rows, capped = future.result()
                    if capped:
                        failures[key] = f"{label}查询被截断（{limit} 行上限）"
                    else:
                        results[key] = rows
                except Exception as error:
                    failures[key] = str(error)

        if "gsv" not in results:
            raise OmnichannelIntelligenceError(failures.get("gsv") or "抖店与视频号小时 GSV 暂未返回")
        gsv_rows = results["gsv"]
        douyin_spend_rows = [
            row
            for key in ("today_total", "today_comparison", "yesterday_comparison", "yesterday_total")
            for row in results.get(f"douyin:{key}", [])
        ]
        wechat_spend_rows = results.get("wechat_spend", [])
        snapshot = build_realtime_snapshot(
            gsv_rows, douyin_spend_rows, wechat_spend_rows, today, current.hour,
        )
        snapshot["quality"] = {
            "warnings": [
                "部分费用来源暂未返回，成交 GSV 仍按根数据展示。" if key != "gsv" else message
                for key, message in failures.items()
            ],
            "failedSources": sorted(failures),
            "preservesMissingAsNull": True,
        }
        if failures:
            snapshot["status"] = "partial"
        self._write(snapshot)
        self._last_error = ""
        return snapshot

    def get_realtime(self, force: bool = False) -> dict[str, Any]:
        now = datetime.now(SHANGHAI_TZ)
        existing = self._load()
        if not force and self._fresh(existing, now):
            return existing or {}
        with self._lock:
            existing = self._load()
            if not force and self._fresh(existing, now):
                return existing or {}
            try:
                return self.refresh(now)
            except Exception as error:
                self._last_error = str(error)
                if existing and (existing.get("channels") or [{}])[0].get("todayDate") == now.date().isoformat():
                    fallback = deepcopy(existing)
                    fallback["status"] = "stale"
                    fallback["staleReason"] = self._last_error
                    return fallback
                if isinstance(error, OmnichannelIntelligenceError):
                    raise
                raise OmnichannelIntelligenceError(str(error)) from error


omnichannel_intelligence_service = OmnichannelIntelligenceService()
