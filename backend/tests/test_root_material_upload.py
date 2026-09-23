import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from app.root_material_upload_service import RootMaterialUploadService


class RootMaterialUploadServiceTests(unittest.TestCase):
    @patch("app.root_material_upload_service.settings")
    @patch("app.root_material_upload_service.FandowDataMcpClient")
    def test_missing_douyin_online_time_is_pending_not_zero(self, client_class, settings):
        settings.fandow_data_mcp_token = "test-token"
        settings.fandow_data_mcp_endpoint = "https://root.example/mcp"
        settings.fandow_data_mcp_timeout_seconds = 10
        client = client_class.return_value
        client.run_sql.side_effect = [
            ([{"source_rows": 46856, "observed_materials": 15, "online_time_materials": 0, "confirmed_materials": 0, "source_updated_at": "2026-08-27 00:21:14"}], False),
            ([{"source_rows": 0, "observed_materials": 0, "online_time_materials": 0, "confirmed_materials": 0, "source_updated_at": None}], False),
            ([{"source_rows": 0, "observed_materials": 0, "online_time_materials": 0, "confirmed_materials": 0, "source_updated_at": None}], False),
            ([{"source_rows": 262, "observed_materials": 262, "confirmed_materials": 262, "source_updated_at": "2026-08-27 02:12:19"}], False),
        ]

        service = RootMaterialUploadService()
        result = service.get_snapshot(date(2026, 8, 26))

        douyin, wechat = result["channels"]
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(douyin["confirmedAssets"])
        self.assertEqual(douyin["state"], "pending")
        self.assertEqual(douyin["observedAssets"], 15)
        self.assertEqual(douyin["metadataCoverage"]["rate"], 0)
        self.assertIn("不能按 0 展示", douyin["note"])
        self.assertEqual(wechat["confirmedAssets"], 262)
        self.assertEqual(wechat["state"], "ready")
        self.assertEqual(result["sourceMode"], "root-data")

    @patch("app.root_material_upload_service.settings")
    @patch("app.root_material_upload_service.FandowDataMcpClient")
    def test_complete_douyin_metadata_returns_verified_count(self, client_class, settings):
        settings.fandow_data_mcp_token = "test-token"
        settings.fandow_data_mcp_endpoint = "https://root.example/mcp"
        settings.fandow_data_mcp_timeout_seconds = 10
        client = client_class.return_value
        client.run_sql.side_effect = [
            ([{"source_rows": 80, "observed_materials": 8, "online_time_materials": 8, "confirmed_materials": 5, "source_updated_at": "2026-08-27 00:00:00"}], False),
            ([{"source_rows": 20, "observed_materials": 2, "online_time_materials": 2, "confirmed_materials": 1, "source_updated_at": "2026-08-27 00:00:01"}], False),
            ([{"source_rows": 0, "observed_materials": 0, "online_time_materials": 0, "confirmed_materials": 0, "source_updated_at": None}], False),
            ([{"source_rows": 3, "observed_materials": 3, "confirmed_materials": 3, "source_updated_at": "2026-08-27 02:00:00"}], False),
        ]

        result = RootMaterialUploadService().get_snapshot(date(2026, 8, 26))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["channels"][0]["confirmedAssets"], 6)
        self.assertEqual(result["channels"][0]["metadataCoverage"]["rate"], 1)


if __name__ == "__main__":
    unittest.main()
