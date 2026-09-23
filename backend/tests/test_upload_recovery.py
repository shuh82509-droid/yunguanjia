import os
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from botocore.exceptions import ClientError
from fastapi import HTTPException
from app.database import Base, SessionLocal, engine
from app.models import UploadSession
from app.oss_service import oss_service
from app.main import complete_multipart_upload_session, create_multipart_upload_session, multipart_upload_session_status
from app.schemas import MultipartUploadCreate


class UploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(engine)
        self.db = SessionLocal()
        self.owner = {"number": "FD-recovery-test", "realName": "测试"}
        self.session = UploadSession(id="recovery-test", owner_number=self.owner["number"], owner_name="测试", sha256="a" * 64,
            file_size=1234, filename="test.mp4", content_type="video/mp4", asset_scope="marketing_video", category="通用",
            object_key="yxb/uploads/test/unique-session-test.mp4", multipart_upload_id="oss-test", part_size=1234,
            status="active", expires_at=datetime.utcnow() + timedelta(hours=1), created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        self.db.add(self.session)
        self.db.commit()
        self.remote = {"object_key": self.session.object_key, "size": 1234, "etag": "verified-etag"}
        self.receipts = {"parts": [{"part_number": 1, "etag": "etag", "size": 1234}]}
        self.missing = ClientError({"Error": {"Code": "NoSuchUpload"}}, "CompleteMultipartUpload")

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(engine)

    def test_lost_complete_receipt_recovered_without_reupload(self):
        for failure in [self.missing, TimeoutError("lost receipt")]:
            self.session.status = "active"
            self.db.commit()
            with patch.object(oss_service, "complete_multipart_upload", side_effect=failure) as complete, patch.object(oss_service, "head_asset", return_value=self.remote):
                result = complete_multipart_upload_session(self.session.id, self.receipts, self.db, self.owner)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(complete.call_count, 1)

    def test_resume_list_parts_missing_detects_already_completed_object(self):
        with patch.object(oss_service, "list_multipart_parts", side_effect=self.missing), patch.object(oss_service, "head_asset", return_value=self.remote):
            result = multipart_upload_session_status(self.session.id, self.db, self.owner)
            self.assertEqual(result["status"], "completed")

    def test_expired_upload_recovers_before_abort_or_new_upload(self):
        self.session.expires_at = datetime.utcnow() - timedelta(hours=1)
        self.db.commit()
        payload = MultipartUploadCreate(filename="test.mp4", content_type="video/mp4", size=1234, sha256="a" * 64, asset_scope="marketing_video", category="通用")
        with patch.object(oss_service, "head_asset", return_value=self.remote), patch.object(oss_service, "abort_multipart_upload") as abort, patch.object(oss_service, "create_multipart_upload") as create:
            self.assertEqual(create_multipart_upload_session(payload, self.db, self.owner)["status"], "completed")
            abort.assert_not_called()
            create.assert_not_called()

    def test_inconclusive_head_keeps_completed_record_and_does_not_reupload(self):
        self.session.status = "completed"
        self.db.commit()
        payload = MultipartUploadCreate(filename="test.mp4", content_type="video/mp4", size=1234, sha256="a" * 64, asset_scope="marketing_video", category="通用")
        with patch.object(oss_service, "head_asset", side_effect=TimeoutError()), patch.object(oss_service, "create_multipart_upload") as create:
            with self.assertRaises(HTTPException) as error:
                create_multipart_upload_session(payload, self.db, self.owner)
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(self.session.status, "completed")
            create.assert_not_called()

    def test_size_mismatch_or_missing_object_is_not_success(self):
        for remote in [None, {**self.remote, "size": 1}, {**self.remote, "object_key": "another-owner.mp4"}]:
            self.session.status = "active"
            self.db.commit()
            with patch.object(oss_service, "complete_multipart_upload", side_effect=self.missing), patch.object(oss_service, "head_asset", return_value=remote):
                with self.assertRaises(HTTPException):
                    complete_multipart_upload_session(self.session.id, self.receipts, self.db, self.owner)
                self.assertEqual(self.session.status, "expired" if remote is None else "active")

    def test_other_owner_cannot_recover_or_probe_object(self):
        with patch.object(oss_service, "head_asset") as head:
            with self.assertRaises(HTTPException) as error:
                multipart_upload_session_status(self.session.id, self.db, {"number": "FD-other"})
            self.assertEqual(error.exception.status_code, 404)
            head.assert_not_called()

    def test_completed_repeated_request_is_idempotent(self):
        self.session.status = "completed"
        self.db.commit()
        with patch.object(oss_service, "complete_multipart_upload") as complete:
            self.assertEqual(complete_multipart_upload_session(self.session.id, {}, self.db, self.owner)["status"], "completed")
            complete.assert_not_called()
