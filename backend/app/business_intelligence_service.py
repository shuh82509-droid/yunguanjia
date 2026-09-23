from __future__ import annotations

import json
import hashlib
import math
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable

from .config import settings
from .personal_sales_service import FandowDataMcpClient, PersonalSalesError, SHANGHAI_TZ
from .business_material_import import material_query_specs, read_material_range
from .business_official_material_import import read_official_range, apply_official_metadata, same_official_scope, MODE as OFFICIAL_MATERIAL_MODE


class BusinessIntelligenceError(PersonalSalesError):
    pass


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round2(value: Any) -> float:
    return round(_number(value) + 1e-9, 2)


def _max_timestamp(current: str, value: Any) -> str:
    candidate = str(value or "")
    return max(current, candidate) if candidate else current


def _product_sql(target: date, channel: str = "douyin") -> str:
    value = target.isoformat()
    following = (target + timedelta(days=1)).isoformat()
    if channel == "wechat":
        shop_filter = "o.shop_id = 1179"
        status_filter = "o.order_status NOT IN ('200', '250')"
    else:
        shop_filter = "o.shop_id IN (1099, 1100)"
        status_filter = "o.order_status <> '4'"
    return f"""WITH RECURSIVE effective_orders AS (
  SELECT o.pay_time, o.platform_order_no, o.sku_items, o.updated_at
  FROM d_root_project_mid_db_brandmarketing.platform_order_item_api_field o FORCE INDEX (idx_shop_pay_time)
  WHERE {shop_filter}
    AND o.pay_time >= TIMESTAMP('{value} 00:00:00')
    AND o.pay_time < TIMESTAMP('{following} 00:00:00')
    AND {status_filter}
),
seq AS (
  SELECT 0 AS n
  UNION ALL
  SELECT n + 1 FROM seq WHERE n < 49
),
sku_lines AS (
  SELECT
    o.platform_order_no,
    JSON_UNQUOTE(JSON_EXTRACT(o.sku_items, CONCAT('$[', seq.n, '].product_id'))) AS product_id,
    JSON_UNQUOTE(JSON_EXTRACT(o.sku_items, CONCAT('$[', seq.n, '].product_name'))) AS product_name,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(o.sku_items, CONCAT('$[', seq.n, '].sku_pay_amount'))) AS UNSIGNED) AS sku_pay_amount,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(o.sku_items, CONCAT('$[', seq.n, '].product_quantity'))) AS UNSIGNED) AS product_quantity,
    o.updated_at
  FROM effective_orders o
  JOIN seq ON seq.n < JSON_LENGTH(o.sku_items)
),
categorized AS (
  SELECT
    platform_order_no, product_id, sku_pay_amount, product_quantity, updated_at,
    CASE
      WHEN product_name LIKE '%水润%'
       AND product_name LIKE '%面膜%'
       AND product_name NOT LIKE '%乳糖酸%'
       AND product_name NOT LIKE '%黑晶%'
       AND COALESCE(product_id, '') NOT IN ('3832934099933331762', '3832385407056740745')
        THEN '水润面膜'
      WHEN product_name REGEXP '晶润紧致眼|眼膜' THEN '晶润紧致眼膜'
      WHEN product_name LIKE '%乳糖酸%' THEN '乳糖酸多效面膜'
      WHEN product_name REGEXP '玻尿酸.*面膜' THEN '玻尿酸极润面膜'
      WHEN product_name LIKE '%黑晶%' THEN '黑晶光蕴面膜'
      WHEN product_name LIKE '%痘肌修护面膜%' THEN '痘肌修护面膜'
      WHEN product_name LIKE '%胜肽菁妍蕴能面膜%' THEN '胜肽菁妍蕴能面膜'
      WHEN product_name LIKE '%燕窝%' AND product_name LIKE '%面膜%' THEN '燕窝面膜'
      WHEN product_name LIKE '%美白针%' OR product_name LIKE '%光感微珠%' THEN '美白针'
      WHEN product_name REGEXP '凝颜.*洁面' THEN '凝颜洁面乳'
      WHEN product_name REGEXP '凝颜.*精粹水' THEN '凝颜精粹水'
      WHEN product_name REGEXP '凝颜.*弹嫩乳|舒缓紧致弹嫩乳' THEN '凝颜紧致弹嫩乳'
      WHEN product_name REGEXP '凝颜.*精粹霜' THEN '凝颜精粹霜'
      WHEN product_name LIKE '%抗老套装%' THEN '抗老护肤套装'
      WHEN product_name LIKE '%凝颜%' THEN '凝颜护肤套装'
      WHEN product_name REGEXP '极润紧肤|极润保湿套装|极润.*水乳|保湿紧肤水' THEN '极润紧肤护肤套装'
      WHEN product_name REGEXP '时光精粹水乳|护肤时光精粹' THEN '时光精粹护肤套装'
      WHEN product_name LIKE '%奢研时光精粹水%' THEN '时光精粹水'
      WHEN product_name LIKE '%深海精粹弹嫩精华%' THEN '深海精粹弹嫩次抛'
      WHEN product_name LIKE '%温润洁面泡沫%' THEN '温润洁面泡沫'
      WHEN product_name LIKE '%温和净透洁面乳%' THEN '温和净透洁面乳'
      WHEN product_name REGEXP '净澈.*黑泥膜' THEN '净澈洁颜黑泥膜'
      WHEN product_name LIKE '%男士清爽洁面乳%' THEN '男士清爽洁面乳'
      WHEN product_name LIKE '%男士劲能畅通套装%' THEN '男士劲能畅通套装'
      WHEN product_name LIKE '%日夜次抛精华%' THEN '日夜次抛精华'
      WHEN product_name LIKE '%重组胶原蛋白次抛精华%' THEN '重组胶原蛋白次抛精华'
      WHEN product_name LIKE '%亲肤润唇膏%' THEN '亲肤润唇膏'
      WHEN product_name LIKE '%柔润奢养抗皱颈膜%' THEN '柔润奢养抗皱颈膜'
      ELSE '其他未归类'
    END AS standard_product_name
  FROM sku_lines
  WHERE sku_pay_amount > 0
),
order_summary AS (
  SELECT COUNT(DISTINCT platform_order_no) AS effective_order_count,
    MAX(updated_at) AS order_source_updated_at
  FROM effective_orders
),
family_sales AS (
  SELECT
    standard_product_name,
    ROUND(SUM(sku_pay_amount) / 100, 2) AS effective_sales_yuan,
    COUNT(DISTINCT platform_order_no) AS order_count,
    SUM(product_quantity) AS product_quantity,
    COUNT(DISTINCT product_id) AS product_link_count,
    MAX(updated_at) AS source_updated_at
  FROM categorized
  GROUP BY standard_product_name
)
SELECT
  DATE('{value}') AS pay_date,
  f.standard_product_name,
  f.effective_sales_yuan,
  f.order_count,
  f.product_quantity,
  f.product_link_count,
  ROUND(f.effective_sales_yuan / NULLIF(SUM(f.effective_sales_yuan) OVER (), 0) * 100, 2) AS sales_share_pct,
  f.source_updated_at,
  o.effective_order_count,
  o.order_source_updated_at
FROM family_sales f
CROSS JOIN order_summary o
ORDER BY f.effective_sales_yuan DESC, f.standard_product_name"""


def _qianchuan_bounds(day: date) -> tuple[str, str]:
    following = day + timedelta(days=1)
    return f"{day.isoformat()} 23:30:00", f"{following.isoformat()} 00:00:00"


def _material_sql(day: date, source: str) -> str:
    value = day.isoformat()
    start, end = _qianchuan_bounds(day)
    if source == "STANDARD":
        table = "d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report"
        name_expression = "NULLIF(m.content_description, '')"
    else:
        table = "d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report"
        name_expression = "COALESCE(NULLIF(m.material_name, ''), NULLIF(m.content_description, ''))"
    return f"""WITH latest_snapshot AS (
  SELECT advertiser_id, MAX(stat_time) AS stat_time
  FROM {table} FORCE INDEX (uk_qc_material_hourly)
  WHERE report_date = DATE('{value}')
    AND stat_time BETWEEN TIMESTAMP('{start}') AND TIMESTAMP('{end}')
    AND advertiser_id NOT IN (SELECT excluded.advertiser_id FROM (SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger WHERE advertiser_name LIKE '电商部达播%' UNION SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost WHERE advertiser_name LIKE '电商部达播%') excluded)
  GROUP BY advertiser_id
)
SELECT '{value}' AS report_date, '{source}' AS source_platform,
  m.advertiser_id, m.material_id,
  MAX({name_expression}) AS material_name,
  NULL AS internal_author,
  NULL AS material_source,
  ROUND(SUM(COALESCE(m.total_pay_order_gmv_include_coupon_for_roi2, m.total_pay_order_gmv_for_roi2, 0)), 2) AS gmv_yuan,
  ROUND(SUM(COALESCE(m.stat_cost_for_roi2, 0)), 2) AS cost_yuan,
  SUM(COALESCE(m.total_pay_order_count_for_roi2, 0)) AS order_count,
  MAX(m.stat_time) AS source_cutoff_at,
  MAX(m.updated_at) AS source_updated_at
FROM latest_snapshot s
JOIN {table} m FORCE INDEX (uk_qc_material_hourly)
  ON m.report_date = DATE('{value}')
 AND m.advertiser_id = s.advertiser_id
 AND m.stat_time = s.stat_time
WHERE COALESCE(NULLIF(m.platform_material_type, ''), 'VIDEO') = 'VIDEO'
  AND m.advertiser_id NOT IN (SELECT excluded.advertiser_id FROM (SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger WHERE advertiser_name LIKE '电商部达播%' UNION SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost WHERE advertiser_name LIKE '电商部达播%') excluded)
GROUP BY m.advertiser_id, m.material_id
HAVING gmv_yuan > 0 OR cost_yuan > 0
ORDER BY gmv_yuan DESC, cost_yuan DESC
LIMIT 200"""


def _material_summary_sql(day: date, source: str) -> str:
    value = day.isoformat()
    start, end = _qianchuan_bounds(day)
    if source == "STANDARD":
        table = "d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report"
        name_expression = "NULLIF(m.content_description, '')"
    else:
        table = "d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report"
        name_expression = "COALESCE(NULLIF(m.material_name, ''), NULLIF(m.content_description, ''))"
    return f"""WITH latest_snapshot AS (
  SELECT advertiser_id, MAX(stat_time) AS stat_time
  FROM {table} FORCE INDEX (uk_qc_material_hourly)
  WHERE report_date = DATE('{value}')
    AND stat_time BETWEEN TIMESTAMP('{start}') AND TIMESTAMP('{end}')
    AND advertiser_id NOT IN (SELECT excluded.advertiser_id FROM (SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger WHERE advertiser_name LIKE '电商部达播%' UNION SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost WHERE advertiser_name LIKE '电商部达播%') excluded)
  GROUP BY advertiser_id
)
SELECT '{value}' AS report_date, '{source}' AS source_platform,
  COUNT(*) AS material_record_count,
  SUM(CASE WHEN COALESCE(m.stat_cost_for_roi2, 0) > 0 THEN 1 ELSE 0 END) AS spent_material_count,
  SUM(CASE WHEN COALESCE(m.stat_cost_for_roi2, 0) > 0
    AND COALESCE(m.total_pay_order_gmv_include_coupon_for_roi2, m.total_pay_order_gmv_for_roi2, 0) > 0
    THEN 1 ELSE 0 END) AS effective_material_count,
  SUM(CASE WHEN COALESCE(m.stat_cost_for_roi2, 0) > 0 AND {name_expression} IS NOT NULL THEN 1 ELSE 0 END)
    AS named_spent_material_count,
  ROUND(SUM(COALESCE(m.total_pay_order_gmv_include_coupon_for_roi2, m.total_pay_order_gmv_for_roi2, 0)), 2)
    AS total_gmv_yuan,
  ROUND(SUM(COALESCE(m.stat_cost_for_roi2, 0)), 2) AS total_cost_yuan,
  SUM(COALESCE(m.total_pay_order_count_for_roi2, 0)) AS total_order_count,
  MAX(m.updated_at) AS daily_source_updated_at
FROM latest_snapshot s
JOIN {table} m FORCE INDEX (uk_qc_material_hourly)
  ON m.report_date = DATE('{value}')
 AND m.advertiser_id = s.advertiser_id
 AND m.stat_time = s.stat_time
WHERE COALESCE(NULLIF(m.platform_material_type, ''), 'VIDEO') = 'VIDEO'
  AND m.advertiser_id NOT IN (SELECT excluded.advertiser_id FROM (SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger WHERE advertiser_name LIKE '电商部达播%' UNION SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost WHERE advertiser_name LIKE '电商部达播%') excluded)
  AND (
    COALESCE(m.stat_cost_for_roi2, 0) > 0
    OR COALESCE(m.total_pay_order_gmv_include_coupon_for_roi2, m.total_pay_order_gmv_for_roi2, 0) > 0
  )"""


def _freshness_sql(target: date) -> str:
    value = target.isoformat()
    return f"""SELECT 'QIANCHUAN_STANDARD' AS source_name, MAX(report_date) AS data_date
FROM d_root_project_mid_db_brandmarketing.qianchuan_material_hourly_report
WHERE report_date <= DATE('{value}')
  AND advertiser_id NOT IN (SELECT excluded.advertiser_id FROM (SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger WHERE advertiser_name LIKE '电商部达播%' UNION SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost WHERE advertiser_name LIKE '电商部达播%') excluded)
UNION ALL
SELECT 'QIANCHUAN_CHENGFANG', MAX(report_date)
FROM d_root_project_mid_db_brandmarketing.qianchuan_chengfang_material_hourly_report
WHERE report_date <= DATE('{value}')
  AND advertiser_id NOT IN (SELECT excluded.advertiser_id FROM (SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_material_asset_ledger WHERE advertiser_name LIKE '电商部达播%' UNION SELECT advertiser_id FROM d_root_project_mid_db_brandmarketing.qianchuan_account_hourly_cost WHERE advertiser_name LIKE '电商部达播%') excluded)"""


def _material_import_summary_sql(day: date, source: str) -> str:
    return _material_summary_sql(day, source).replace(
        'COUNT(*) AS material_record_count,',
        '(SELECT COUNT(*) FROM latest_snapshot) AS snapshot_account_count,\n'
        '  (SELECT MAX(stat_time) FROM latest_snapshot) AS source_cutoff_at,\n'
        '  COUNT(*) AS material_record_count,',
    )


def external_material_query_specs(target: date) -> dict[str, dict[str, Any]]:
    return material_query_specs(target, _material_import_summary_sql, _material_sql)


def build_overview_snapshot(
    product_rows: list[dict[str, Any]],
    material_rows: list[dict[str, Any]],
    target: date,
    days: int,
    freshness: dict[str, str],
    material_summary_rows: list[dict[str, Any]] | None = None,
    wechat_product_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    products = []
    product_total = 0.0
    product_quantity = 0
    product_source_updated_at = ""
    effective_order_count: int | None = None
    for row in product_rows:
        sales = _round2(row.get("effective_sales_yuan"))
        product_total += sales
        product_quantity += int(_number(row.get("product_quantity")))
        if effective_order_count is None and row.get("effective_order_count") is not None:
            effective_order_count = int(_number(row.get("effective_order_count")))
        product_source_updated_at = _max_timestamp(product_source_updated_at, row.get("source_updated_at"))
        product_source_updated_at = _max_timestamp(product_source_updated_at, row.get("order_source_updated_at"))
        products.append({
            "standardProductName": str(row.get("standard_product_name") or "其他未归类"),
            "effectiveSalesYuan": sales,
            "orderCount": int(_number(row.get("order_count"))),
            "productQuantity": int(_number(row.get("product_quantity"))),
            "productLinkCount": int(_number(row.get("product_link_count"))),
            "salesSharePct": _round2(row.get("sales_share_pct")),
            "mapped": str(row.get("standard_product_name") or "") != "其他未归类",
        })
    products.sort(key=lambda item: (-item["effectiveSalesYuan"], item["standardProductName"]))
    unclassified_sales = sum(item["effectiveSalesYuan"] for item in products if not item["mapped"])

    wechat_products = []
    wechat_product_total = 0.0
    wechat_product_quantity = 0
    wechat_product_source_updated_at = ""
    wechat_effective_order_count: int | None = None
    for row in wechat_product_rows or []:
        sales = _round2(row.get("effective_sales_yuan"))
        wechat_product_total += sales
        wechat_product_quantity += int(_number(row.get("product_quantity")))
        if wechat_effective_order_count is None and row.get("effective_order_count") is not None:
            wechat_effective_order_count = int(_number(row.get("effective_order_count")))
        wechat_product_source_updated_at = _max_timestamp(
            wechat_product_source_updated_at, row.get("source_updated_at"),
        )
        wechat_product_source_updated_at = _max_timestamp(
            wechat_product_source_updated_at, row.get("order_source_updated_at"),
        )
        wechat_products.append({
            "standardProductName": str(row.get("standard_product_name") or "其他未归类"),
            "effectiveSalesYuan": sales,
            "orderCount": int(_number(row.get("order_count"))),
            "productQuantity": int(_number(row.get("product_quantity"))),
            "productLinkCount": int(_number(row.get("product_link_count"))),
            "salesSharePct": _round2(row.get("sales_share_pct")),
            "mapped": str(row.get("standard_product_name") or "") != "其他未归类",
        })
    wechat_products.sort(key=lambda item: (-item["effectiveSalesYuan"], item["standardProductName"]))
    wechat_unclassified_sales = sum(
        item["effectiveSalesYuan"] for item in wechat_products if not item["mapped"]
    )

    material_map: dict[str, dict[str, Any]] = {}
    unresolved_material_rows = []
    for row in material_rows:
        material_id = str(row.get("material_id") or "").strip()
        if not material_id or re.fullmatch(r'-?\d+', material_id) and int(material_id) <= 0:
            unresolved_material_rows.append(row)
            continue
        source = str(row.get("source_platform") or "").strip()
        advertiser_id = str(row.get("advertiser_id") or "").strip()
        key = material_id or f"missing:{source}:{advertiser_id}:{row.get('report_date') or ''}"
        current = material_map.setdefault(key, {
            "materialId": material_id,
            "materialName": "",
            "internalAuthor": "",
            "materialSource": "",
            "sourcePlatforms": set(),
            "advertiserIds": set(),
            "gmvYuan": 0.0,
            "costYuan": 0.0,
            "orderCount": 0,
            "activeDates": set(),
            "dailyGmv": {},
            "sourceCutoffAt": "",
            "sourceUpdatedAt": "",
        })
        name = str(row.get("material_name") or "").strip()
        if name and (not current["materialName"] or len(name) > len(current["materialName"])):
            current["materialName"] = name
        author = str(row.get("internal_author") or "").strip()
        if author:
            current["internalAuthor"] = author
        material_source = str(row.get("material_source") or "").strip()
        if material_source:
            current["materialSource"] = material_source
        if source:
            current["sourcePlatforms"].add(source)
        if advertiser_id:
            current["advertiserIds"].add(advertiser_id)
        gmv = _number(row.get("gmv_yuan"))
        cost = _number(row.get("cost_yuan"))
        report_date = str(row.get("report_date") or "")[:10]
        current["gmvYuan"] += gmv
        current["costYuan"] += cost
        current["orderCount"] += int(_number(row.get("order_count")))
        if report_date:
            current["activeDates"].add(report_date)
            current["dailyGmv"][report_date] = current["dailyGmv"].get(report_date, 0.0) + gmv
        current["sourceCutoffAt"] = _max_timestamp(current["sourceCutoffAt"], row.get("source_cutoff_at"))
        current["sourceUpdatedAt"] = _max_timestamp(current["sourceUpdatedAt"], row.get("source_updated_at"))

    materials = []
    for item in material_map.values():
        cost = _round2(item["costYuan"])
        gmv = _round2(item["gmvYuan"])
        materials.append({
            "materialId": item["materialId"],
            "materialName": item["materialName"],
            "internalAuthor": item["internalAuthor"],
            "materialSource": item["materialSource"],
            "sourcePlatforms": sorted(item["sourcePlatforms"]),
            "advertiserIds": sorted(item["advertiserIds"]),
            "gmvYuan": gmv,
            "costYuan": cost,
            "roi": round(gmv / cost, 4) if cost > 0 else None,
            "orderCount": item["orderCount"],
            "activeDayCount": len(item["activeDates"]),
            "maxDailyGmvYuan": _round2(max(item["dailyGmv"].values(), default=0)),
            "sourceCutoffAt": item["sourceCutoffAt"],
            "sourceUpdatedAt": item["sourceUpdatedAt"],
        })
    positive_materials = [item for item in materials if item["gmvYuan"] > 0]
    top_materials = sorted(
        positive_materials,
        key=lambda item: (-item["gmvYuan"], -item["costYuan"], item["materialId"]),
    )[:10]
    if material_summary_rows:
        material_record_count = int(sum(_number(row.get("material_record_count")) for row in material_summary_rows))
        spent_material_count = int(sum(_number(row.get("spent_material_count")) for row in material_summary_rows))
        effective_material_count = int(sum(_number(row.get("effective_material_count")) for row in material_summary_rows))
        named_spent_material_count = int(sum(_number(row.get("named_spent_material_count")) for row in material_summary_rows))
        material_gmv = _round2(sum(_number(row.get("total_gmv_yuan")) for row in material_summary_rows))
        material_cost = _round2(sum(_number(row.get("total_cost_yuan")) for row in material_summary_rows))
        source_updated_at = max((str(row.get("daily_source_updated_at") or "") for row in material_summary_rows), default="")
    else:
        spent_materials = [item for item in materials if item["costYuan"] > 0]
        material_record_count = len(materials)
        spent_material_count = len(spent_materials)
        effective_material_count = len([item for item in spent_materials if item["gmvYuan"] > 0])
        named_spent_material_count = len([item for item in spent_materials if item["materialName"]])
        material_gmv = _round2(sum(item["gmvYuan"] for item in materials))
        material_cost = _round2(sum(item["costYuan"] for item in materials))
        source_updated_at = max((item["sourceUpdatedAt"] for item in materials), default="")
    candidate_gmv = _round2(sum(item['gmvYuan'] for item in materials))
    candidate_gmv_coverage = round(candidate_gmv / material_gmv, 4) if material_gmv > 0 else None
    period_start = target - timedelta(days=days - 1)
    generated_at = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

    return {
        "schemaVersion": 1,
        "status": "ready" if product_rows and (material_summary_rows or material_rows) else "partial",
        "generatedAt": generated_at,
        "query": {
            "productDate": target.isoformat(),
            "materialStartDate": period_start.isoformat(),
            "materialEndDate": target.isoformat(),
            "materialDays": days,
        },
        "summary": {
            "productEffectiveSalesYuan": _round2(product_total) if product_rows else None,
            "effectiveOrderCount": effective_order_count if product_rows else None,
            "productQuantity": product_quantity if product_rows else None,
            "wechatProductEffectiveSalesYuan": _round2(wechat_product_total) if wechat_product_rows else None,
            "wechatEffectiveOrderCount": wechat_effective_order_count if wechat_product_rows else None,
            "wechatProductQuantity": wechat_product_quantity if wechat_product_rows else None,
            "materialAttributedGmvYuan": material_gmv if (material_summary_rows or material_rows) else None,
            "materialCostYuan": material_cost if (material_summary_rows or material_rows) else None,
            "materialRoi": round(material_gmv / material_cost, 4) if material_cost > 0 else None,
            "spentMaterialCount": spent_material_count if (material_summary_rows or material_rows) else None,
            "effectiveMaterialCount": effective_material_count if (material_summary_rows or material_rows) else None,
            "effectiveMaterialRate": round(effective_material_count / spent_material_count, 4) if spent_material_count else None,
        },
        "products": products,
        "wechatProducts": wechat_products,
        "topMaterials": top_materials,
        # Internal candidate ledger for registered-material matching. The public
        # business endpoint removes this field and continues to return TOP10.
        "_materialCandidates": materials,
        "quality": {
            "unclassifiedProductSalesYuan": _round2(unclassified_sales) if product_rows else None,
            "productMappedSalesRate": round((product_total - unclassified_sales) / product_total, 4) if product_total > 0 else None,
            "wechatUnclassifiedProductSalesYuan": _round2(wechat_unclassified_sales) if wechat_product_rows else None,
            "wechatProductMappedSalesRate": (
                round((wechat_product_total - wechat_unclassified_sales) / wechat_product_total, 4)
                if wechat_product_total > 0 else None
            ),
            "materialNameCoverageRate": round(named_spent_material_count / spent_material_count, 4) if spent_material_count else None,
            "materialSourceRowCount": material_record_count,
            "materialUnresolvedCandidateRowCount": len(unresolved_material_rows),
            "materialUnresolvedCandidateGmvYuan": _round2(sum(_number(row.get('gmv_yuan')) for row in unresolved_material_rows)),
            "topCandidateGmvCoverageRate": candidate_gmv_coverage,
            "topMaterialLineageLinkedCount": None,
            "topMaterialLineageCoverageRate": None,
        },
        "coverage": {
            "product": {
                "state": "available" if product_rows else "unavailable",
                "sourceUpdatedAt": product_source_updated_at or None,
                "source": "FanDo 根数据 · WIS 抖店订单 SKU 明细",
            },
            "wechatProduct": {
                "state": "available" if wechat_product_rows else "unavailable",
                "sourceUpdatedAt": wechat_product_source_updated_at or None,
                "source": "FanDo 根数据 · WIS 视频号店铺订单 SKU 明细",
            },
            "material": {
                "state": "available" if (material_summary_rows or material_rows) else "unavailable",
                "sourceUpdatedAt": source_updated_at or None,
                "sourceFreshness": freshness,
                "source": "FanDo 根数据 · 千川标准与乘方素材小时报表",
            },
            "warnings": [
                message for condition, message in (
                    (not product_rows, "所选日期没有返回可核验的单品成交明细，未按 0 处理"),
                    (not wechat_product_rows, "所选日期没有返回可核验的视频号单品成交明细，未按 0 处理"),
                    (not (material_summary_rows or material_rows), "所选周期没有返回可核验的千川素材数据，未按 0 处理"),
                    (bool(product_rows and unclassified_sales > 0), "其他未归类单品仍保留在总额与质量提示中"),
                    (bool(candidate_gmv_coverage is not None and candidate_gmv_coverage < 0.9), "每日高成交候选覆盖不足全部素材归因 GMV 的 90%，TOP10 仅作发现线索"),
                    (bool(unresolved_material_rows), "源报表有未映射素材编号（例如 -2）：其金额保留在完整汇总中，不将占位编号列为可办理视频"),
                ) if condition
            ],
        },
        "definitions": {
            "productEffectiveSales": "北京时间自然日内 WIS 抖店有效订单的 SKU 实付金额，排除 order_status=4 的取消单；金额由分换算为元",
            "wechatProductEffectiveSales": "北京时间自然日内 WIS 视频号店铺有效订单的 SKU 实付金额，排除 order_status=200/250 的无效状态；金额由分换算为元",
            "materialAttributedGmv": "千川 ROI2 含券成交 GMV；含券字段为空时回退 ROI2 成交 GMV，与根数据有效成交额不是同一口径",
            "effectiveMaterialRate": f"所选{days}日账户素材-天记录中，产生千川归因 GMV 的有消耗记录数 ÷ 全部有消耗记录数；这是可守恒的投放记录效率，不等同于去重成片成功率",
            "topMaterialDiscovery": f"每天分别取千川标准与乘方的高成交候选，再按素材ID合并所选{days}日表现生成TOP10；候选GMV覆盖率随数据质量一起展示",
            "lineageCoverage": "TOP10 素材中，可与中枢推送记录及 WIS 素材库资产匹配的数量占比；未匹配不等于没有来源",
        },
    }


def _iso_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(SHANGHAI_TZ) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def external_product_query_specs(target: date) -> dict[str, dict[str, Any]]:
    return {
        key: {"sql": sql, "sqlSha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(), "limit": 500}
        for key, channel in (("product", "douyin"), ("wechatProduct", "wechat"))
        for sql in [_product_sql(target, channel)]
    }


def validate_external_product_import(value: Any, target: date, now: datetime) -> dict[str, Any]:
    """Validate an operator-published read-only import. No HTTP import API exists.

    The containing directory is owned by the deployment operator, outside asset
    uploads. SQL hashes bind each query to this exact date and source contract;
    they are provenance checks, not credentials or an authorization mechanism.
    """
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("sourceMode") != "authorized-root-query-import":
        raise BusinessIntelligenceError("外部根数据导入格式不正确")
    if value.get("scope") != {"department": "品牌营销部", "authorizedBy": "FD-026222"}:
        raise BusinessIntelligenceError("外部根数据导入不属于已授权部门范围")
    if value.get("realBusinessDate") != target.isoformat():
        raise BusinessIntelligenceError("外部根数据导入业务日期不匹配")
    external_at = _iso_time(value.get("externalReadAt"))
    if not external_at or external_at > now + timedelta(minutes=5):
        raise BusinessIntelligenceError("外部根数据读取时间缺失或晚于当前时间")
    queries = value.get("queries")
    if not isinstance(queries, dict) or not queries or set(queries) - {"product", "wechatProduct"}:
        raise BusinessIntelligenceError("外部根数据查询集合不正确")
    expected = external_product_query_specs(target)
    for key, query in queries.items():
        if not isinstance(query, dict) or query.get("sqlSha256") != expected[key]["sqlSha256"]:
            raise BusinessIntelligenceError(f"{key} SQL 校验值不匹配")
        rows = query.get("rows")
        if not isinstance(rows, list) or len(rows) >= 500 or type(query.get("rowCount")) is not int or query["rowCount"] != len(rows) or query.get("truncated") is not False:
            raise BusinessIntelligenceError(f"{key} 查询结果不完整或被截断")
        read_at = _iso_time(query.get("readAt"))
        if not read_at or read_at > external_at or read_at.date() <= target:
            raise BusinessIntelligenceError(f"{key} 完整自然日读取时间不正确")
        names = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("pay_date") != target.isoformat():
                raise BusinessIntelligenceError(f"{key} 行业务日期不匹配")
            name = row.get("standard_product_name")
            if not isinstance(name, str) or not name.strip() or name in names:
                raise BusinessIntelligenceError(f"{key} 产品分类缺失或重复")
            names.add(name)
            for field in ("effective_sales_yuan", "order_count", "product_quantity", "product_link_count", "sales_share_pct", "effective_order_count"):
                raw = row.get(field)
                try:
                    number = float(raw)
                except (ValueError, TypeError):
                    number = float("nan")
                if isinstance(raw, bool) or not math.isfinite(number) or number < 0:
                    raise BusinessIntelligenceError(f"{key} {field} 不是有效的非负数字")
                if field in {"order_count", "product_quantity", "product_link_count", "effective_order_count"} and not number.is_integer():
                    raise BusinessIntelligenceError(f"{key} {field} 不是整数")
            if not row.get("source_updated_at") or not row.get("order_source_updated_at"):
                raise BusinessIntelligenceError(f"{key} 源更新时间缺失")
    return deepcopy(value)


def _refresh_error(error: Exception) -> dict[str, str]:
    text = str(error)
    if re.search(r"登录.*(?:失效|无效)|身份|Unauthorized|\b40[13]\b", text, re.I):
        return {"state": "authorization_invalid", "message": "服务器原根数据 MCP 授权已失效；合法外部只读导入可独立更新，不代表该凭证已恢复"}
    if re.search(r"timeout|timed out|超时", text, re.I):
        return {"state": "timeout", "message": "根数据查询超时，已保留同日期来源事实"}
    if "尚未" in text or "未配置" in text:
        return {"state": "pending", "message": text[:200]}
    return {"state": "failed", "message": re.sub(r"Bearer\s+\S+|https?://\S+", "[已隐藏]", text)[:200] or "根数据查询失败"}


class BusinessIntelligenceService:
    # Independent source ownership prevents a late material batch from freezing
    # both product channels, or a product refresh from relabelling old materials.
    _fields = {
        "product": (["products"], ["productEffectiveSalesYuan", "effectiveOrderCount", "productQuantity"], ["unclassifiedProductSalesYuan", "productMappedSalesRate"]),
        "wechatProduct": (["wechatProducts"], ["wechatProductEffectiveSalesYuan", "wechatEffectiveOrderCount", "wechatProductQuantity"], ["wechatUnclassifiedProductSalesYuan", "wechatProductMappedSalesRate"]),
        "material": (["topMaterials", "_materialCandidates"], ["materialAttributedGmvYuan", "materialCostYuan", "materialRoi", "spentMaterialCount", "effectiveMaterialCount", "effectiveMaterialRate"], ["materialNameCoverageRate", "materialSourceRowCount", "materialUnresolvedCandidateRowCount", "materialUnresolvedCandidateGmvYuan", "topCandidateGmvCoverageRate", "topMaterialLineageLinkedCount", "topMaterialLineageCoverageRate"]),
    }

    def __init__(self, snapshot_path: str | Path | None = None,
                 client_factory: Callable[..., FandowDataMcpClient] = FandowDataMcpClient,
                 import_directory: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.snapshot_path = Path(snapshot_path or settings.business_intelligence_snapshot_path)
        self.import_directory = Path(import_directory) if import_directory else self.snapshot_path.parent / "business-intelligence-imports"
        self.official_import_directory = self.import_directory.parent / "business-intelligence-official"
        existing_material = (self._read_json(self.snapshot_path) or {}).get('coverage', {}).get('material', {})
        self._last_official_policy = deepcopy(existing_material.get('importCoverage')) if existing_material.get('sourceMode') == OFFICIAL_MATERIAL_MODE else None
        self.client_factory = client_factory
        self._clock = clock or (lambda: datetime.now(SHANGHAI_TZ))
        self._refresh_lock = Lock()
        self._material_import_cache: dict[str, Any] = {}
        self._last_attempt_at: datetime | None = None
        self._last_error = ""
        self._attempts = self._read_json(self.snapshot_path.with_name(self.snapshot_path.stem + ".refresh-state.json")) or {}

    @property
    def configured(self) -> bool:
        return bool(settings.fandow_data_mcp_token and settings.fandow_data_mcp_endpoint)

    @staticmethod
    def expected_target_date(now: datetime | None = None) -> date:
        current = now.astimezone(SHANGHAI_TZ) if now else datetime.now(SHANGHAI_TZ)
        return current.date() - timedelta(days=1)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _dated_path(self, target: date, days: int) -> Path:
        return self.snapshot_path.with_name(self.snapshot_path.stem + ".daily") / f"{target.isoformat()}-{days}.json"

    def _load_snapshot(self, target: date | None = None, days: int = 7) -> dict[str, Any] | None:
        if target:
            dated = self._read_json(self._dated_path(target, days))
            if self._matches(dated, target, days):
                return dated
        legacy = self._read_json(self.snapshot_path)
        return legacy if not target or self._matches(legacy, target, days) else None

    def _write_snapshot(self, snapshot: dict[str, Any]) -> None:
        query = snapshot["query"]
        target = date.fromisoformat(query["productDate"])
        self._atomic_write(self._dated_path(target, query["materialDays"]), snapshot)
        latest = self._load_snapshot()
        if not latest or (latest.get("query") or {}).get("productDate", "") <= target.isoformat():
            self._atomic_write(self.snapshot_path, snapshot)

    @staticmethod
    def _matches(snapshot: dict[str, Any] | None, target: date, days: int) -> bool:
        query = (snapshot or {}).get("query") or {}
        return query.get("productDate") == target.isoformat() and query.get("materialDays") == days

    def _key(self, target: date, days: int) -> str:
        return f"{target.isoformat()}:{days}"

    def _import(self, target: date) -> dict[str, Any] | None:
        path = self.import_directory / f"{target.isoformat()}.json"
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise BusinessIntelligenceError("外部根数据文件不是受限大小的普通导入文件")
        return validate_external_product_import(self._read_json(path), target, self._clock())

    def _material_import(self, target: date, days: int) -> dict[str, Any]:
        signature = []
        official_manifest = self.official_import_directory / 'manifest.json'
        try:
            stat = official_manifest.lstat()
            signature.append(('official_manifest', stat.st_mtime_ns, stat.st_size, stat.st_mode))
            manifest = self._read_json(official_manifest) if stat.st_size <= 200_000 and not official_manifest.is_symlink() else None
            for day in _date_range(target - timedelta(days=days-1), target):
                item = (manifest or {}).get('snapshots', {}).get(str(day), {})
                name = item.get('file', '')
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.[0-9a-f]{64}\.json', name):
                    try:
                        stat = (self.official_import_directory / name).lstat()
                        signature.append((name, stat.st_mtime_ns, stat.st_size, stat.st_mode))
                    except OSError:
                        signature.append((name, None))
        except OSError:
            signature.append(('official_manifest', None))
        for day in _date_range(target - timedelta(days=days-1), target):
            path = self.import_directory / f'{day.isoformat()}-material.json'
            try:
                stat = path.lstat()
                signature.append((stat.st_mtime_ns, stat.st_size, stat.st_mode))
            except OSError:
                signature.append(None)
        key = self._key(target, days)
        cached = self._material_import_cache.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        value = read_official_range(self.official_import_directory, target, days, self._clock())
        if value is None and self._last_official_policy:
            coverage = deepcopy(self._last_official_policy)
            coverage.update(state='partial', startDate=str(target-timedelta(days=days-1)), endDate=str(target), requestedDays=days,
                            missingDays=[{'reason': '已启用的官方 manifest 暂时不可读，保留上次范围'}])
            value = {'rows': [], 'summaries': [], 'freshness': {}, 'readAt': None, 'sourceMode': OFFICIAL_MATERIAL_MODE,
                     'officialScopeEnforced': True, 'importCoverage': coverage}
        if value is not None and value.get('officialScopeEnforced'):
            if value['importCoverage'].get('scopeFingerprint'):
                self._last_official_policy = deepcopy(value['importCoverage'])
            elif self._last_official_policy:
                value['importCoverage']['scopeFingerprint'] = self._last_official_policy.get('scopeFingerprint')
                value['importCoverage']['preservesLastVerifiedPolicy'] = True
        if value is None:
            value = read_material_range(self.import_directory, target, days, self._clock(), external_material_query_specs)
        if len(self._material_import_cache) >= 3:
            self._material_import_cache.pop(next(iter(self._material_import_cache)))
        self._material_import_cache[key] = (signature, value)
        return value

    def _with_automation(self, snapshot: dict[str, Any], requested: date, days: int) -> dict[str, Any]:
        result = deepcopy(snapshot)
        material_policy = self._material_import(requested, days)
        if material_policy.get('officialScopeEnforced') and not same_official_scope(result.get('coverage', {}).get('material', {}), material_policy):
            # An old 17-account root cache cannot become a 16-account official result.
            blank = self._empty(requested, days)
            arrays, summaries, qualities = self._fields['material']
            for field in arrays:
                result[field] = []
            for field in summaries + ['materialActualPayGmvYuan', 'materialCouponInclusiveGmvYuan']:
                result['summary'][field] = None
            for field in qualities:
                result['quality'][field] = blank['quality'].get(field)
            result['coverage']['material'] = {'state': 'unavailable', 'sourceMode': OFFICIAL_MATERIAL_MODE,
                'source': '千川官方 · 16账户自然日视频报表', 'importCoverage': deepcopy(material_policy['importCoverage']),
                'scopeFingerprint': material_policy['importCoverage'].get('scopeFingerprint'),
                'lastError': {'state': 'pending', 'message': '正在核验所选周期的官方数据；旧范围金额未混入'}}
            result['status'] = 'partial'
        attempt = self._attempts.get(self._key(requested, days), {})
        result["automation"] = {
            "configured": self.configured,
            "requestedProductDate": requested.isoformat(), "requestedMaterialDays": days,
            "matchesRequest": self._matches(snapshot, requested, days),
            "lastSuccessAt": snapshot.get("generatedAt"),
            "lastAttemptAt": attempt.get("lastAttemptAt"), "lastError": attempt.get("lastError", ""),
            "refreshing": self._refresh_lock.locked(),
            "retryIntervalMinutes": settings.business_intelligence_retry_interval_minutes,
            "preservesLastSuccessOnFailure": True,
            "sourceStates": deepcopy(snapshot.get("coverage", {})),
            "serverConnectionState": attempt.get("serverConnectionState", "not_verified"),
        }
        if not snapshot.get("generatedAt"):
            latest = self._load_snapshot()
            if latest:
                result["automation"]["lastAvailableSnapshot"] = {
                    "businessDate": (latest.get("query") or {}).get("productDate"),
                    "generatedAt": latest.get("generatedAt"),
                }
        return result

    def _empty(self, target: date, days: int) -> dict[str, Any]:
        value = build_overview_snapshot([], [], target, days, {})
        value["generatedAt"] = None
        value["status"] = "partial"
        return value

    def _assemble(self, target: date, days: int, sources: dict[str, Any], errors: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        material = sources.get("material", {})
        value = build_overview_snapshot(sources.get("product", {}).get("rows", []), material.get("rows", []), target, days,
            material.get("freshness", {}), material.get("summaries", []), sources.get("wechatProduct", {}).get("rows", []))
        reads = []
        for key, (arrays, summaries, qualities) in self._fields.items():
            source = sources.get(key)
            coverage = value["coverage"][key]
            coverage["realBusinessDate"] = target.isoformat()
            if source:
                coverage.update({k: deepcopy(source[k]) for k in ("sourceMode", "externalReadAt", "sqlSha256", "readAt", "importCoverage") if k in source})
                coverage["lastSuccessAt"] = source["readAt"]
                reads.append(source["readAt"])
                continue
            old = (previous or {}).get("coverage", {}).get(key, {})
            policy = sources.get('_officialPolicy', {}) if key == 'material' else {}
            if old.get("state") in {"available", "stale"} and (not policy.get('officialScopeEnforced') or same_official_scope(old, policy)):
                for field in arrays:
                    value[field] = deepcopy(previous.get(field, []))
                for field in summaries:
                    value["summary"][field] = previous.get("summary", {}).get(field)
                if key == 'material':
                    for field in ('materialActualPayGmvYuan', 'materialCouponInclusiveGmvYuan'):
                        value['summary'][field] = previous.get('summary', {}).get(field)
                for field in qualities:
                    value["quality"][field] = previous.get("quality", {}).get(field)
                value["coverage"][key] = coverage = deepcopy(old)
                coverage["state"] = "stale"
                old_read = old.get("readAt") or previous.get("generatedAt")
                if old_read:
                    reads.append(old_read)
            error = errors.get(key, {"state": "pending", "message": "来源等待读取"})
            coverage["lastError"] = error
            value["coverage"]["warnings"].append(f"{key}：{error['message']}")
        value["generatedAt"] = max(reads, key=lambda item: _iso_time(item) or datetime.min.replace(tzinfo=SHANGHAI_TZ)) if reads else None
        states = [value["coverage"][key]["state"] for key in self._fields]
        value["status"] = "stale" if "stale" in states else "ready" if all(state == "available" for state in states) else "partial"
        if material.get('sourceMode') == OFFICIAL_MATERIAL_MODE:
            apply_official_metadata(value, material)
        return value

    def refresh(self, target: date, days: int = 7) -> dict[str, Any]:
        if days < 1 or days > 30 or target > self.expected_target_date(self._clock()):
            raise BusinessIntelligenceError("仅支持截至昨天的完整自然日，素材周期为 1 至 30 天")
        previous = self._load_snapshot(target, days)
        sources: dict[str, Any] = {}
        errors: dict[str, Any] = {}
        try:
            imported = self._import(target)
            if imported:
                for key, query in imported["queries"].items():
                    sources[key] = {**query, "sourceMode": "authorized-root-query-import", "externalReadAt": imported["externalReadAt"]}
        except Exception as error:
            errors["externalImport"] = _refresh_error(error)
        material_import = self._material_import(target, days)
        if material_import.get('officialScopeEnforced'):
            sources['_officialPolicy'] = material_import
        if material_import['importCoverage']['state'] == 'complete':
            sources['material'] = material_import
        client = None
        try:
            if not self.configured:
                raise BusinessIntelligenceError("服务器根数据 MCP 连接尚未配置")
            client = self.client_factory(settings.fandow_data_mcp_endpoint, settings.fandow_data_mcp_token, settings.fandow_data_mcp_timeout_seconds)
            client.initialize()
        except Exception as error:
            errors["serverConnection"] = _refresh_error(error)
            client = None
        for key, channel in (("product", "douyin"), ("wechatProduct", "wechat")):
            if key in sources:
                continue
            try:
                if client is None:
                    raise BusinessIntelligenceError(errors["serverConnection"]["message"])
                sql = _product_sql(target, channel)
                rows, capped = client.run_sql(f"WIS {channel} 单品有效成交 {target.isoformat()}", sql, 500)
                if capped:
                    raise BusinessIntelligenceError("单品有效成交查询被截断")
                sources[key] = {"rows": rows, "readAt": self._clock().isoformat(timespec="seconds"), "sourceMode": "server-root-mcp", "sqlSha256": hashlib.sha256(sql.encode()).hexdigest()}
            except Exception as error:
                errors[key] = errors.get("serverConnection") or _refresh_error(error)
        # Make independent product facts readable before slower material queries.
        provisional = self._assemble(target, days, sources, errors, previous)
        if sources:
            self._write_snapshot(provisional)
        if 'material' not in sources:
            try:
                if material_import.get('officialScopeEnforced'):
                    raise BusinessIntelligenceError('所选官方素材周期尚未全日闭合；不与其他范围的根快照混合')
                if client is None:
                    raise BusinessIntelligenceError(errors["serverConnection"]["message"])
                fresh_rows, capped = client.run_sql("千川素材新鲜度", _freshness_sql(target), 20)
                if capped:
                    raise BusinessIntelligenceError("千川素材新鲜度查询被截断")
                freshness = {str(row.get("source_name") or ""): str(row.get("data_date") or "")[:10] for row in fresh_rows}
                if any(freshness.get(key, "") < target.isoformat() for key in ("QIANCHUAN_STANDARD", "QIANCHUAN_CHENGFANG")):
                    raise BusinessIntelligenceError(f"千川素材尚未完整到达 {target.isoformat()}，不影响独立产品数据")
                rows, summaries = [], []
                for day in _date_range(target - timedelta(days=days - 1), target):
                    for source in ("STANDARD", "CHENGFANG"):
                        summary, capped = client.run_sql(f"{source} 素材汇总 {day.isoformat()}", _material_summary_sql(day, source), 10)
                        if capped:
                            raise BusinessIntelligenceError("素材汇总被截断")
                        candidates, capped = client.run_sql(f"{source} 素材候选 {day.isoformat()}", _material_sql(day, source), 300)
                        if capped:
                            raise BusinessIntelligenceError("素材候选被截断")
                        summaries.extend(summary)
                        rows.extend(candidates)
                sources["material"] = {"rows": rows, "summaries": summaries, "freshness": freshness, "readAt": self._clock().isoformat(timespec="seconds"), "sourceMode": "server-root-mcp"}
            except Exception as error:
                errors["material"] = errors.get("serverConnection") or _refresh_error(error)
        authorization_error = next((error for error in errors.values() if error["state"] == "authorization_invalid"), None)
        if authorization_error:
            errors["serverConnection"] = authorization_error
        value = self._assemble(target, days, sources, errors, previous)
        if 'material' not in sources:
            value['coverage']['material']['importCoverage'] = material_import['importCoverage']
        if sources or value.get("generatedAt"):
            self._write_snapshot(value)
        self._last_error = "；".join(dict.fromkeys(e["message"] for e in errors.values()))
        key = self._key(target, days)
        verified_server_query = any(source.get('sourceMode') == 'server-root-mcp' for source in sources.values())
        previous_connection = self._attempts.get(key, {}).get('serverConnectionState')
        if not self._last_error and not verified_server_query and previous_connection == 'authorization_invalid':
            self._last_error = self._attempts[key].get('lastError') or _refresh_error(BusinessIntelligenceError('403'))['message']
        self._attempts[key] = {"lastAttemptAt": self._clock().isoformat(timespec="seconds"),
            "lastSuccessAt": value.get("generatedAt"), "lastError": self._last_error,
            "serverConnectionState": errors.get("serverConnection", {}).get("state",
                'available' if verified_server_query else previous_connection if previous_connection == 'authorization_invalid' else 'not_verified')}
        self._atomic_write(self.snapshot_path.with_name(self.snapshot_path.stem + ".refresh-state.json"), self._attempts)
        return value

    def _needs_refresh(self, snapshot: dict[str, Any] | None, target: date, days: int) -> bool:
        attempt = self._attempts.get(self._key(target, days), {})
        last = _iso_time(attempt.get("lastAttemptAt"))
        try:
            imported = self._import(target)
            if imported and any(_iso_time(q["readAt"]) > (_iso_time((snapshot or {}).get("coverage", {}).get(key, {}).get("readAt")) or datetime.min.replace(tzinfo=SHANGHAI_TZ)) for key, q in imported["queries"].items()):
                return True
        except BusinessIntelligenceError:
            pass
        material_import = self._material_import(target, days)
        material_read = _iso_time(material_import.get('readAt'))
        if material_import.get('officialScopeEnforced') and material_import['importCoverage']['state'] == 'complete' and not same_official_scope((snapshot or {}).get('coverage', {}).get('material', {}), material_import):
            return True
        if material_import['importCoverage']['state'] == 'complete' and material_read and material_read > (_iso_time((snapshot or {}).get('coverage', {}).get('material', {}).get('readAt')) or datetime.min.replace(tzinfo=SHANGHAI_TZ)):
            return True
        if last and self._clock() - last < timedelta(minutes=settings.business_intelligence_retry_interval_minutes):
            return False
        read = _iso_time((snapshot or {}).get("generatedAt"))
        return not read or self._clock() - read >= timedelta(minutes=settings.business_intelligence_retry_interval_minutes)

    def _run_refresh(self, requested: date, days: int) -> dict[str, Any]:
        try:
            return self.refresh(requested, days)
        except Exception as error:
            message = _refresh_error(error)
            self._attempts[self._key(requested, days)] = {"lastAttemptAt": self._clock().isoformat(timespec="seconds"), "lastError": message["message"]}
            # A corrupt/unwritable cache must not leave the refresh lock stuck.
            return self._load_snapshot(requested, days) or self._empty(requested, days)
        finally:
            self._refresh_lock.release()

    def get_overview(self, target: date | None = None, days: int = 7, force: bool = False) -> dict[str, Any]:
        requested = target or self.expected_target_date(self._clock())
        if requested > self.expected_target_date(self._clock()) or days < 1 or days > 30:
            raise BusinessIntelligenceError("仅支持截至昨天的完整自然日，素材周期为 1 至 30 天")
        existing = self._load_snapshot(requested, days)
        if not force and not self._needs_refresh(existing, requested, days):
            return self._with_automation(existing or self._empty(requested, days), requested, days)
        if self._refresh_lock.acquire(blocking=force):
            if force:
                return self._with_automation(self._run_refresh(requested, days), requested, days)
            Thread(target=self._run_refresh, args=(requested, days), name="business-root-refresh", daemon=True).start()
        return self._with_automation(self._load_snapshot(requested, days) or self._empty(requested, days), requested, days)

    def refresh_if_due(self, now: datetime | None = None) -> dict[str, Any] | None:
        target = self.expected_target_date(now or self._clock())
        snapshot = self._load_snapshot(target, 7)
        if not self._needs_refresh(snapshot, target, 7):
            return self._with_automation(snapshot or self._empty(target, 7), target, 7)
        return self.get_overview(target, 7, force=True)


business_intelligence_service = BusinessIntelligenceService()
