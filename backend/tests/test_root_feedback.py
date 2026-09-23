import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from app.root_feedback_service import RootFeedbackService


class RootFeedbackServiceTests(unittest.TestCase):
    def service_with_rows(self, rows):
        service = RootFeedbackService()
        client = Mock()
        client.run_sql.return_value = (rows, False)
        return service, client

    def test_adq_uses_exact_video_grain_and_keeps_missing_rows_unavailable(self):
        rows = [{
            "stat_date": "2026-08-19",
            "video_id": "video-1",
            "material_id": "material-1",
            "material_id_count": 1,
            "cost_yuan": "0.00",
            "view_count": 0,
            "valid_click_count": 0,
            "order_amount_yuan": "0.00",
            "order_net_amount_yuan": "0.00",
            "account_count": 1,
        }]
        service, client = self.service_with_rows(rows)
        with patch.object(service, "_client", return_value=client):
            result = service.adq_material_metrics_batch(
                ["video-1", "video-missing"],
                start_date="2026-08-01",
                end_date="2026-08-19",
            )

        self.assertTrue(result["video-1"]["has_data"])
        self.assertEqual(result["video-1"]["material_id"], "material-1")
        self.assertEqual(result["video-1"]["material_id_status"], "verified")
        self.assertEqual(result["video-1"]["metrics"]["cost_yuan"], 0)
        self.assertIsNone(result["video-1"]["metrics"]["order_roi"])
        self.assertFalse(result["video-missing"]["has_data"])
        self.assertEqual(result["video-missing"]["material_id_status"], "missing")
        self.assertEqual(result["video-missing"]["metrics"], {})
        self.assertIn("不按 0 处理", result["video-missing"]["message"])
        sql = client.run_sql.call_args.args[1]
        self.assertIn("video_asset_id", sql)

    def test_adq_does_not_guess_when_root_returns_multiple_material_ids(self):
        rows = [{
            "stat_date": "2026-08-19",
            "video_id": "video-1",
            "material_id": "material-2",
            "material_id_count": 2,
            "cost_yuan": "1.00",
            "view_count": 1,
            "valid_click_count": 1,
            "order_amount_yuan": "0.00",
            "order_net_amount_yuan": "0.00",
            "account_count": 1,
        }]
        service, client = self.service_with_rows(rows)
        with patch.object(service, "_client", return_value=client):
            result = service.adq_material_metrics_batch(
                ["video-1"], start_date="2026-08-01", end_date="2026-08-19"
            )

        self.assertEqual(result["video-1"]["material_id"], "")
        self.assertEqual(result["video-1"]["material_id_status"], "ambiguous")
        self.assertIn("人工核验", result["video-1"]["message"])

    def test_channels_rejects_stale_export_id_and_uses_unique_account_time_content_match(self):
        rows = [
            {
                "stat_date": "2026-08-13",
                "finder_account_name": "小钱的科学护肤日记",
                "feed_id": "old-feed",
                "export_id": "export/stale",
                "published_at": "2026-08-13 19:13:14",
                "video_name": "旧内容",
                "play_count": 67,
                "thumb_like_count": 0,
                "heart_like_count": 0,
                "pay_gmv_yuan": "0.00",
            },
            {
                "stat_date": "2026-08-20",
                "finder_account_name": "小钱的科学护肤日记",
                "feed_id": "new-feed",
                "export_id": "export/new",
                "published_at": "2026-08-20 16:32:00",
                "video_name": "爱自己就要给自己用最好的",
                "play_count": 321,
                "thumb_like_count": 4,
                "heart_like_count": 2,
                "pay_gmv_yuan": "19.90",
            },
        ]
        service, client = self.service_with_rows(rows)
        item = {
            "id": "task-1",
            "account_name": "小钱的科学护肤日记",
            "title": "8.19-燕窝-dx",
            "description": "爱自己就要给自己用最好的",
            "filename": "8.19-燕窝-dx.mp4",
            "platform_content_id": "export/stale",
            "platform_export_id": "export/stale",
            "created_at": datetime(2026, 8, 20, 8, 30),
        }
        with patch.object(service, "_client", return_value=client):
            result = service.channels_delivery_batch([item])

        self.assertEqual(result["task-1"]["platform_content_id"], "new-feed")
        self.assertEqual(result["task-1"]["platform_export_id"], "export/new")
        self.assertEqual(result["task-1"]["like_count"], 6)
        self.assertEqual(result["task-1"]["gmv_fen"], 1990)

    def test_channels_does_not_guess_when_two_candidates_are_ambiguous(self):
        rows = [
            {
                "stat_date": "2026-08-20",
                "finder_account_name": "同一账号",
                "feed_id": f"feed-{index}",
                "export_id": f"export/{index}",
                "published_at": f"2026-08-20 16:3{index}:00",
                "video_name": "同一条发布文案",
                "play_count": 1,
                "thumb_like_count": 0,
                "heart_like_count": 0,
                "pay_gmv_yuan": "0.00",
            }
            for index in (1, 2)
        ]
        service, client = self.service_with_rows(rows)
        item = {
            "id": "task-ambiguous",
            "account_name": "同一账号",
            "title": "同一条发布文案",
            "description": "",
            "filename": "same.mp4",
            "created_at": datetime(2026, 8, 20, 8, 30),
        }
        with patch.object(service, "_client", return_value=client):
            result = service.channels_delivery_batch([item])

        self.assertNotIn("task-ambiguous", result)


if __name__ == "__main__":
    unittest.main()
