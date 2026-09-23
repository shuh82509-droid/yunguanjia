import unittest
from datetime import datetime
from types import SimpleNamespace
from app.workflow_receipts import receipt_payload


class ReceiptProjectionTests(unittest.TestCase):
    def row(self, **changes):
        data = dict(id="task-1", asset_id=12, status="success", created_by_number="TEST-1", updated_at=datetime(2026, 9, 5), advertiser_id="a", plan_id="p", platform_asset_id="v", binding_verified_at=None, binding_evidence={}, account_id="account", platform_export_id="export", platform_export_verified_at=None, platform_content_url="https://example.test/video", password="must-not-leak", error_message="private-error")
        return SimpleNamespace(**{**data, **changes})

    def test_success_label_does_not_invent_verification(self):
        result = receipt_payload(self.row(), "qianchuan")
        self.assertIsNone(result["binding_verified_at"])
        self.assertIsNone(result["binding_evidence"]["matched_count"])

    def test_only_allowlisted_evidence_leaves_service(self):
        result = receipt_payload(self.row(binding_evidence={"matched_count": 1, "video_id": "v", "token": "private"}), "qianchuan")
        self.assertEqual(result["binding_evidence"]["matched_count"], 1)
        self.assertNotIn("token", result["binding_evidence"])
        self.assertNotIn("password", result)
        self.assertNotIn("error_message", result)

    def test_channels_retains_pending_readback(self):
        result = receipt_payload(self.row(status="submitted"), "wechat_channels")
        self.assertEqual(result["status"], "submitted")
        self.assertIsNone(result["platform_export_verified_at"])

    def test_invalid_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            receipt_payload(self.row(), "unknown")


if __name__ == "__main__":
    unittest.main()
