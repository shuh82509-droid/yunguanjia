import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from app.main import business_intelligence_omnichannel_realtime
from app.omnichannel_intelligence_service import build_realtime_snapshot


class OmnichannelSnapshotTests(unittest.TestCase):
    def test_complete_hour_comparison_and_department_total_match_root_dashboard_rules(self):
        gsv_rows = [
            {"channel_key": "douyin", "pay_date": "2026-08-25", "hour_no": 12, "gsv_yuan": 100},
            {"channel_key": "douyin", "pay_date": "2026-08-26", "hour_no": 12, "gsv_yuan": 120},
            {"channel_key": "douyin", "pay_date": "2026-08-26", "hour_no": 13, "gsv_yuan": 30},
            {"channel_key": "wechat", "pay_date": "2026-08-25", "hour_no": 12, "gsv_yuan": 200},
            {"channel_key": "wechat", "pay_date": "2026-08-26", "hour_no": 12, "gsv_yuan": 240},
            {"channel_key": "wechat", "pay_date": "2026-08-26", "hour_no": 13, "gsv_yuan": 60},
        ]
        douyin_spend = [
            {"report_date": "2026-08-25", "hour_no": 12, "spend_yuan": 40},
            {"report_date": "2026-08-26", "hour_no": 12, "spend_yuan": 48},
            {"report_date": "2026-08-26", "hour_no": 13, "spend_yuan": 12},
        ]
        wechat_spend = [
            {
                "stat_date": "2026-08-26",
                "spend_yuan": 80,
                "adq_spend_yuan": 50,
                "shop_promotion_spend_yuan": 10,
                "creator_commission_spend_yuan": 10,
                "wechat_bean_spend_yuan": 10,
                "has_adq_data": 1,
                "has_shop_promotion_data": 1,
                "has_commission_data": 1,
                "has_wechat_bean_data": 1,
            }
        ]

        result = build_realtime_snapshot(gsv_rows, douyin_spend, wechat_spend, date(2026, 8, 26), 13)
        douyin, wechat = result["channels"]

        self.assertEqual(douyin["comparisonThroughHour"], 12)
        self.assertEqual(douyin["todayTotalYuan"], 150)
        self.assertEqual(douyin["todayComparisonYuan"], 120)
        self.assertIsNone(douyin["points"][13]["todayHourlyYuan"])
        self.assertEqual(wechat["todayTotalYuan"], 300)
        self.assertEqual(result["summary"]["departmentTodayGsvYuan"], 450)
        self.assertEqual(wechat["spendCoverage"], "complete")
        self.assertIn("不重复计入", result["definitions"]["qianchuanAttribution"])

    def test_material_cutoff_spend_is_never_reported_as_complete_cost(self):
        gsv_rows = [
            {"channel_key": "douyin", "pay_date": "2026-08-25", "hour_no": 9, "gsv_yuan": 80},
            {"channel_key": "douyin", "pay_date": "2026-08-26", "hour_no": 9, "gsv_yuan": 100},
        ]
        cutoff_spend = [
            {"cutoff_key": "today_total", "spend_yuan": 20},
            {"cutoff_key": "today_comparison", "spend_yuan": 20},
            {"cutoff_key": "yesterday_comparison", "spend_yuan": 18},
            {"cutoff_key": "yesterday_total", "spend_yuan": 30},
        ]

        result = build_realtime_snapshot(gsv_rows, cutoff_spend, [], date(2026, 8, 26), 10)
        douyin = result["channels"][0]

        self.assertEqual(douyin["spendCoverage"], "partial")
        self.assertEqual(douyin["yesterdaySpendCoverage"], "partial")
        self.assertEqual(douyin["spendBreakdown"][0]["label"], "千川可见素材")
        self.assertIn("部分覆盖", result["definitions"]["spendCoverage"])

    def test_missing_channel_is_unavailable_instead_of_zero(self):
        result = build_realtime_snapshot([], [], [], date(2026, 8, 26), 10)

        self.assertIsNone(result["summary"]["departmentTodayGsvYuan"])
        for channel in result["channels"]:
            self.assertIsNone(channel["todayTotalYuan"])
            self.assertEqual(channel["state"], "missing")
            self.assertTrue(channel["unavailableReason"])


class OmnichannelEndpointTests(unittest.TestCase):
    @patch("app.main.omnichannel_intelligence_service.get_realtime")
    @patch("app.main.require_module_access")
    def test_endpoint_uses_shared_data_dashboard_permission(self, require_access: MagicMock, get_realtime: MagicMock):
        get_realtime.return_value = {"channels": [], "status": "ready"}
        db = MagicMock()
        user = {"number": "FD-TEST", "status": "normal"}

        result = business_intelligence_omnichannel_realtime(force=False, db=db, user=user)

        require_access.assert_called_once_with(user, "data-dashboard", db)
        get_realtime.assert_called_once_with(force=False)
        self.assertEqual(result["status"], "ready")


if __name__ == "__main__":
    unittest.main()
