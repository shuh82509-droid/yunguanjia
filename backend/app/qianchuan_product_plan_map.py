"""Reviewed WIS product-to-Qianchuan account and plan mappings.

This is a read-only application snapshot of the Feishu source documents.  The
frontend uses it to prepare a target preview; it never submits a delivery by
itself.
"""

from copy import deepcopy


_SOURCE = {
    "title": "WIS千川在用账户",
    "url": "https://jqx28l0j4lx.feishu.cn/wiki/FJhZwqaxPionKHkOhQGc16tFnBc",
    "document_id": "BX1nd6H4yo6hIqxUKvvcRKUMn8b",
    "revision": 536,
    "verified_at": "2026-09-03T14:03:28+08:00",
    "department_account_source": {
        "title": "（部门-账户/店铺）映射关系",
        "url": "https://jqx28l0j4lx.feishu.cn/wiki/VYZzwh2RsiyER6kuJe2cvNibn5c",
        "revision": 475,
        "rule": "巨量千川账户名中包含 WIS",
    },
}


def _rule(
    advertiser_id: str,
    advertiser_name: str,
    *,
    keyword: str = "",
    plan_id: str = "",
    scope: str = "multiplication",
    match_mode: str = "keyword",
) -> dict:
    return {
        "advertiser_id": advertiser_id,
        "advertiser_name": advertiser_name,
        "scope": scope,
        "match_mode": "exact_plan" if plan_id else match_mode,
        "keyword": keyword,
        "plan_id": plan_id,
    }


_ITEMS = [
    {
        "key": "deep_sea_ampoule",
        "label": "深海次抛",
        "aliases": ["深海次抛精华", "深海次抛", "次抛"],
        "rules": [
            _rule("1760222277505102", "营销部WIS-博观11-WIS官旗-推商品", keyword="次抛"),
            _rule("1758788274050062", "营销部WIS-佳云-眼膜", keyword="次抛"),
            _rule("1864699162217866", "营销部WIS-仟得2（眼膜直播小号）", keyword="次抛"),
        ],
    },
    {
        "key": "black_crystal_mask",
        "label": "黑晶光蕴面膜",
        "aliases": ["黑晶光蕴面膜", "黑晶面膜", "黑晶", "黑金"],
        "rules": [
            _rule(
                "1869672250595328",
                "营销部WIS-厚拓（爱创）-2",
                plan_id="1870289794646204",
            ),
            _rule("1760222277505102", "营销部WIS-博观11-WIS官旗-推商品", keyword="黑晶"),
            _rule("1758788274050062", "营销部WIS-佳云-眼膜", keyword="黑晶"),
            _rule("1850659758537995", "营销部WIS-佳云万合-新1--眼膜", keyword="黑晶"),
        ],
    },
    {
        "key": "bird_nest_mask",
        "label": "燕窝面膜",
        "aliases": ["燕窝胜肽面膜", "燕窝面膜", "燕窝"],
        "rules": [
            _rule(
                "1869672250595328",
                "营销部WIS-厚拓（爱创）-2",
                plan_id="1873739958166842",
            ),
            _rule("1760222277505102", "营销部WIS-博观11-WIS官旗-推商品", keyword="燕窝"),
            _rule("1850659758537995", "营销部WIS-佳云万合-新1--眼膜", keyword="燕窝"),
            _rule("1758788274050062", "营销部WIS-佳云-眼膜", keyword="燕窝"),
            _rule("1864699162217866", "营销部WIS-仟得2（眼膜直播小号）", keyword="燕窝"),
        ],
    },
    {
        "key": "whitening_ampoule",
        "label": "美白针",
        "aliases": ["光感微珠美白精华", "美白精华", "美白针"],
        "rules": [
            _rule("1760222277505102", "营销部WIS-博观11-WIS官旗-推商品", keyword="美白针"),
            _rule("1758788274050062", "营销部WIS-佳云-眼膜", keyword="美白针"),
        ],
    },
    {
        "key": "protein_spray",
        "label": "肌活蛋白喷雾",
        "aliases": ["WIS肌活蛋白喷雾", "肌活蛋白喷雾", "蛋白喷雾", "喷雾"],
        "rules": [
            _rule(
                "1869672250595328",
                "营销部WIS-厚拓（爱创）-2",
                plan_id="1873739958166842",
            ),
            _rule("1760222277505102", "营销部WIS-博观11-WIS官旗-推商品", keyword="喷雾"),
            _rule("1758788274050062", "营销部WIS-佳云-眼膜", keyword="喷雾"),
            _rule("1864699162217866", "营销部WIS-仟得2（眼膜直播小号）", keyword="喷雾"),
        ],
    },
]


def product_plan_map_snapshot() -> dict:
    """Return an isolated copy so request handlers cannot mutate the source."""

    return {"source": deepcopy(_SOURCE), "items": deepcopy(_ITEMS)}
