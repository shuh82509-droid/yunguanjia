import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
import unittest
from unittest.mock import patch, MagicMock
from app.qianchuan_service import QianchuanService, QianchuanError


class PushReconciliationTests(unittest.TestCase):
    def test_verified_material_list_skips_stale_plan_detail_and_all_writes(self):
        service = QianchuanService()
        evidence = {"matched_count": 1, "advertiser_id": "100", "plan_id": "200", "video_id": "300", "readback_source": "material/get"}
        with patch.object(service, "plan_video_evidence", return_value=evidence) as readback, patch.object(service, "plan_detail") as detail, patch.object(service, "_authorized_request") as write, patch.object(service, "_video_creative_material") as cover:
            result = service.add_to_plan(None, advertiser_id="100", plan_id="200", video_id="300")
            self.assertTrue(result["already_present"])
            self.assertEqual(result["binding_evidence"], evidence)
            readback.assert_called_once_with(None, advertiser_id="100", plan_id="200", video_id="300")
            detail.assert_not_called()
            cover.assert_not_called()
            write.assert_not_called()

    def test_unavailable_readback_never_becomes_a_duplicate_post(self):
        service = QianchuanService()
        with patch.object(service, "plan_video_evidence", return_value={"matched_count": 0, "readback_errors": ["timeout"]}), patch.object(service, "_authorized_request") as write:
            with self.assertRaises(QianchuanError) as error:
                service.add_to_plan(None, advertiser_id="100", plan_id="200", video_id="300")
            self.assertEqual(error.exception.category, "plan_verification_pending")
            write.assert_not_called()

    def test_verified_absence_allows_one_binding_and_requires_readback(self):
        service = QianchuanService()
        evidence = {"matched_count": 1, "video_id": "300"}
        with patch.object(service, "plan_video_evidence", return_value={"matched_count": 0, "readback_errors": []}), patch.object(service, "plan_detail", return_value={"marketing_goal": "LIVE_PROM_GOODS"}), patch.object(service, "_video_creative_material", return_value={"video_id": "300", "video_cover_id": "cover"}), patch.object(service, "_authorized_request", return_value={"request_id": "test"}) as write, patch.object(service, "_wait_for_plan_video", return_value=evidence) as verify:
            result = service.add_to_plan(None, advertiser_id="100", plan_id="200", video_id="300")
            self.assertFalse(result["already_present"])
            write.assert_called_once()
            verify.assert_called_once()

    def test_single_unrelated_video_is_not_used_as_requested_video_cover(self):
        service = QianchuanService()
        db = MagicMock()
        db.get.return_value = None
        db.scalars.return_value.all.return_value = []
        with patch.object(service, "_authorized_request", return_value={"data": {"list": [{"id": "wrong-id", "poster_url": "https://example.test/other.jpg"}]}}) as request:
            with self.assertRaises(QianchuanError) as error:
                service._video_creative_material(db, advertiser_id="100", video_id="300")
            self.assertEqual(error.exception.category, "video_detail_pending")
            self.assertEqual(request.call_count, 1)
