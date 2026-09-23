import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.business_intelligence_service import build_overview_snapshot
from app.main import business_intelligence_overview


class BusinessIntelligenceSnapshotTests(unittest.TestCase):
    def test_snapshot_keeps_metric_boundaries_and_explicit_effectiveness_denominator(self):
        product_rows = [
            {
                "standard_product_name": "水润面膜",
                "effective_sales_yuan": "1000.00",
                "order_count": 10,
                "product_quantity": 12,
                "product_link_count": 2,
                "sales_share_pct": 80,
                "source_updated_at": "2026-08-21 01:00:00",
                "effective_order_count": 11,
            },
            {
                "standard_product_name": "其他未归类",
                "effective_sales_yuan": "250.00",
                "order_count": 2,
                "product_quantity": 2,
                "product_link_count": 1,
                "sales_share_pct": 20,
                "source_updated_at": "2026-08-21 01:00:00",
                "effective_order_count": 11,
            },
        ]
        material_rows = [
            {
                "report_date": "2026-08-20",
                "source_platform": "STANDARD",
                "advertiser_id": "a1",
                "material_id": "m1",
                "material_name": "有效素材.mp4",
                "internal_author": "",
                "material_source": "E_COMMERCE",
                "gmv_yuan": 600,
                "cost_yuan": 200,
                "order_count": 6,
                "source_updated_at": "2026-08-21 00:05:00",
            },
            {
                "report_date": "2026-08-20",
                "source_platform": "STANDARD",
                "advertiser_id": "a1",
                "material_id": "m2",
                "material_name": "已消耗未成交.mp4",
                "gmv_yuan": 0,
                "cost_yuan": 100,
                "order_count": 0,
                "source_updated_at": "2026-08-21 00:05:00",
            },
            {
                "report_date": "2026-08-20",
                "source_platform": "CHENGFANG",
                "advertiser_id": "a2",
                "material_id": "m3",
                "material_name": "无消耗归因记录.mp4",
                "gmv_yuan": 50,
                "cost_yuan": 0,
                "order_count": 1,
                "source_updated_at": "2026-08-21 00:06:00",
            },
        ]

        result = build_overview_snapshot(
            product_rows,
            material_rows,
            date(2026, 8, 20),
            7,
            {"QIANCHUAN_STANDARD": "2026-08-20", "QIANCHUAN_CHENGFANG": "2026-08-20"},
            wechat_product_rows=[{
                "standard_product_name": "水润面膜",
                "effective_sales_yuan": "800.00",
                "order_count": 8,
                "product_quantity": 9,
                "product_link_count": 1,
                "sales_share_pct": 100,
                "source_updated_at": "2026-08-21 01:05:00",
                "effective_order_count": 8,
            }],
        )

        self.assertEqual(result["summary"]["productEffectiveSalesYuan"], 1250)
        self.assertEqual(result["summary"]["effectiveOrderCount"], 11)
        self.assertEqual(result["summary"]["materialAttributedGmvYuan"], 650)
        self.assertEqual(result["summary"]["spentMaterialCount"], 2)
        self.assertEqual(result["summary"]["effectiveMaterialCount"], 1)
        self.assertEqual(result["summary"]["effectiveMaterialRate"], 0.5)
        self.assertEqual(len(result["_materialCandidates"]), 3)
        self.assertEqual(result["quality"]["unclassifiedProductSalesYuan"], 250)
        self.assertEqual(result["quality"]["productMappedSalesRate"], 0.8)
        self.assertEqual(result["summary"]["wechatProductEffectiveSalesYuan"], 800)
        self.assertEqual(result["wechatProducts"][0]["standardProductName"], "水润面膜")
        self.assertEqual(result["coverage"]["wechatProduct"]["state"], "available")
        self.assertIn("不是同一口径", result["definitions"]["materialAttributedGmv"])

    def test_missing_source_rows_are_unavailable_not_zero(self):
        result = build_overview_snapshot([], [], date(2026, 8, 20), 7, {})

        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["summary"]["productEffectiveSalesYuan"])
        self.assertIsNone(result["summary"]["materialAttributedGmvYuan"])
        self.assertEqual(result["coverage"]["product"]["state"], "unavailable")
        self.assertTrue(result["coverage"]["warnings"])


class BusinessIntelligenceEndpointTests(unittest.TestCase):
    @patch("app.main.business_intelligence_service.get_overview")
    @patch("app.main.require_module_access")
    def test_endpoint_enforces_data_dashboard_scope(self, require_access: MagicMock, get_overview: MagicMock):
        get_overview.return_value = {"topMaterials": [], "quality": {}}
        db = MagicMock()

        result = business_intelligence_overview(
            target_date=date(2026, 8, 20),
            days=7,
            db=db,
            user={"number": "FD-TEST", "status": "normal"},
        )

        require_access.assert_called_once_with(
            {"number": "FD-TEST", "status": "normal"}, "data-dashboard", db,
        )
        self.assertEqual(result["topMaterials"], [])

    @patch("app.main.require_module_access")
    def test_endpoint_returns_permission_denial(self, require_access: MagicMock):
        require_access.side_effect = HTTPException(403, "当前账号未开通根数据看板访问权限")

        with self.assertRaises(HTTPException) as raised:
            business_intelligence_overview(
                target_date=date(2026, 8, 20),
                days=7,
                db=MagicMock(),
                user={"number": "FD-NO", "status": "normal"},
            )

        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
