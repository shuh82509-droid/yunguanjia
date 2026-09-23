"""Offline auth/queue regressions. No real tokens or upstream calls are used."""
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import AppMeta, Asset, QianchuanDelivery
from app.qianchuan_service import QianchuanService, QianchuanError, _meta, _set_meta
from app import main


class QianchuanAuthReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "test.db"), connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.service = QianchuanService()
        self.env = patch.dict(os.environ, {"QIANCHUAN_APP_ID": "12345", "QIANCHUAN_APP_SECRET": "test-secret", "QIANCHUAN_ACCESS_TOKEN": "", "QIANCHUAN_REFRESH_TOKEN": "", "QIANCHUAN_TOKEN_SAVED_AT": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.seed()

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def seed(self, valid=False):
        with self.sessions() as db:
            db.query(AppMeta).delete()
            for key, value in {"access_token": "test-access", "refresh_token": "test-refresh", "expires_in": "3600", "refresh_expires_in": "2592000", "saved_at": (datetime.now(timezone.utc) - timedelta(hours=0 if valid else 2)).isoformat(), "authorization_invalid": "0"}.items():
                _set_meta(db, "qianchuan." + key, value)
            db.commit()

    def read(self, key):
        with self.sessions() as db:
            return _meta(db, "qianchuan." + key)

    @staticmethod
    def fresh(prefix="new"):
        return {"access_token": prefix + "-access", "refresh_token": prefix + "-refresh", "expires_in": 86400, "refresh_token_expires_in": 2592000}

    def call_token(self):
        with self.sessions() as db:
            return self.service.token(db)

    def test_temporary_and_unknown_errors_never_erase_credentials(self):
        errors = [QianchuanError("服务内部错误，请稍后重试"), QianchuanError("HTTP 503", code=503, retryable=True), QianchuanError("timeout", category="read_timeout", retryable=True), QianchuanError("limit", category="rate_limit", code=429, retryable=True), QianchuanError("未知错误", code=99999), QianchuanError("app secret invalid", code=401)]
        for error in errors:
            with self.subTest(error=str(error)):
                self.seed()
                with patch.object(self.service, "_request_json", side_effect=error) as remote:
                    for _ in range(2):
                        with self.assertRaises(QianchuanError) as caught:
                            self.call_token()
                        self.assertEqual(caught.exception.category, "authorization_temporary")
                    remote.assert_called_once()
                self.assertEqual(self.read("access_token"), "test-access")
                self.assertEqual(self.read("refresh_token"), "test-refresh")
                self.assertEqual(self.read("authorization_invalid"), "0")

    def test_explicit_refresh_revocation_blocks_without_deleting_tokens(self):
        with patch.object(self.service, "_request_json", side_effect=QianchuanError("refresh_token已过期")) as remote:
            for _ in range(2):
                with self.assertRaises(QianchuanError) as caught:
                    self.call_token()
                self.assertEqual(caught.exception.category, "authorization_required")
            remote.assert_called_once()
        self.assertEqual(self.read("authorization_invalid"), "1")
        self.assertEqual(self.read("refresh_token"), "test-refresh")

    def test_concurrent_refresh_is_single_flight_across_instances(self):
        started, release = threading.Event(), threading.Event()
        def remote(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return {"data": self.fresh()}
        def worker():
            with self.sessions() as db:
                return QianchuanService().token(db)
        with patch.object(QianchuanService, "_request_json", side_effect=remote) as request, ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker) for _ in range(8)]
            self.assertTrue(started.wait(5))
            release.set()
            self.assertEqual([f.result(10) for f in futures], ["new-access"] * 8)
            request.assert_called_once()

    def test_new_oauth_credentials_win_over_stale_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.seed()
                started, release = threading.Event(), threading.Event()
                def remote(*args, **kwargs):
                    started.set()
                    self.assertTrue(release.wait(5))
                    if fail:
                        raise QianchuanError("refresh_token已失效")
                    return {"data": self.fresh("stale")}
                with patch.object(self.service, "_request_json", side_effect=remote), ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(self.call_token)
                    self.assertTrue(started.wait(5))
                    with self.sessions() as db:
                        QianchuanService()._store_token(db, self.fresh("oauth"))
                    release.set()
                    self.assertEqual(result.result(10), "oauth-access")
                self.assertEqual(self.read("refresh_token"), "oauth-refresh")
                self.assertEqual(self.read("authorization_invalid"), "0")

    def test_cooldown_persists_and_later_success_clears_it(self):
        with patch.object(self.service, "_request_json", side_effect=QianchuanError("server")):
            with self.assertRaises(QianchuanError):
                self.call_token()
        with self.sessions() as db, patch.object(QianchuanService, "_request_json") as remote:
            replacement = QianchuanService()
            status = replacement.validated_status(db)
            self.assertTrue(status["authorized"])
            self.assertTrue(status["dispatch_paused"])
            self.assertEqual(status["authorization_state"], "refresh_temporarily_unavailable")
            remote.assert_not_called()
            _set_meta(db, "qianchuan.refresh_retry_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
            db.commit()
        with patch.object(self.service, "_request_json", return_value={"data": self.fresh()}) as remote:
            self.assertEqual(self.call_token(), "new-access")
            remote.assert_called_once()
        self.assertEqual(self.read("refresh_error"), "")
        self.assertEqual(self.read("refresh_retry_at"), "")

    def test_old_invalid_state_never_falls_back_to_environment(self):
        with self.sessions() as db:
            _set_meta(db, "qianchuan.authorization_invalid", "1")
            _set_meta(db, "qianchuan.access_token", "")
            _set_meta(db, "qianchuan.refresh_token", "")
            db.commit()
        with patch.dict(os.environ, {"QIANCHUAN_ACCESS_TOKEN": "old-env", "QIANCHUAN_REFRESH_TOKEN": "old-env-refresh"}), patch.object(self.service, "_request_json") as remote:
            with self.assertRaises(QianchuanError):
                self.call_token()
            remote.assert_not_called()
        self.assertEqual(self.read("authorization_invalid"), "1")

    def test_missing_or_malformed_refresh_data_preserves_tokens(self):
        for data in ({}, {"access_token": "x"}, {"access_token": "x", "expires_in": "bad"}):
            self.seed()
            with patch.object(self.service, "_request_json", return_value={"data": data}):
                with self.assertRaises(QianchuanError):
                    self.call_token()
            self.assertEqual(self.read("access_token"), "test-access")

    def test_diagnostics_redact_echoed_credentials(self):
        with patch.object(self.service, "_request_json", side_effect=QianchuanError("test-access test-refresh test-secret")):
            with self.assertRaises(QianchuanError):
                self.call_token()
        self.assertEqual(self.read("refresh_error"), "[redacted] [redacted] [redacted]")

    def test_omitted_refresh_token_does_not_extend_original_lifetime(self):
        with self.sessions() as db:
            before = self.service._refresh_valid_until(db)
        response = {"data": {"access_token": "new-access", "expires_in": 86400}}
        with patch.object(self.service, "_request_json", return_value=response):
            self.call_token()
        with self.sessions() as db:
            self.assertEqual(self.service._refresh_valid_until(db), before)
            self.assertEqual(self.service._refresh_token(db), "test-refresh")

    def add_tasks(self):
        with self.sessions() as db:
            asset = Asset(object_key="auth-test.mp4", filename="auth-test.mp4", media_type="video")
            db.add(asset); db.flush()
            for index, status in enumerate(("pending", "failed", "partial")):
                db.add(QianchuanDelivery(id=status, asset_id=asset.id, created_by_number="TEST-ONLY", advertiser_id=str(100 + index), batch_id="auth-test", status=status, platform_asset_id="existing-video", upload_task_id="original-upload", attempt_count=2))
            db.commit()

    def test_scheduler_and_direct_worker_preserve_pending_and_history(self):
        self.add_tasks()
        with self.sessions() as db:
            _set_meta(db, "qianchuan.authorization_invalid", "1"); db.commit()
        with patch.object(main, "SessionLocal", self.sessions), patch.object(main, "qianchuan_service", self.service), patch.object(self.service, "_request_json") as remote:
            self.assertEqual(main.claim_qianchuan_tasks(3), [])
            main.run_qianchuan_push(["pending"])
            remote.assert_not_called()
        with self.sessions() as db:
            for status in ("pending", "failed", "partial"):
                task = db.get(QianchuanDelivery, status)
                self.assertEqual(task.status, status)
                self.assertEqual(task.attempt_count, 2)
                self.assertEqual(task.platform_asset_id, "existing-video")
                self.assertEqual(task.upload_task_id, "original-upload")

    def test_auth_restored_claims_pending_only_not_failed_or_partial_history(self):
        self.seed(valid=True); self.add_tasks()
        with patch.object(main, "SessionLocal", self.sessions), patch.object(main, "qianchuan_service", self.service):
            self.assertEqual(main.claim_qianchuan_tasks(3), ["pending"])

    def test_mid_bind_refresh_outage_preserves_remote_checkpoint(self):
        self.seed(valid=True); self.add_tasks()
        with self.sessions() as db:
            task = db.get(QianchuanDelivery, "pending")
            task.plan_id, task.plan_type = "123", "multiplication"
            db.commit()
        with patch.object(main, "SessionLocal", self.sessions), patch.object(main, "qianchuan_service", self.service), patch.object(self.service, "begin_video_upload") as upload, patch.object(self.service, "add_to_plan", side_effect=QianchuanError("续期暂时失败", category="authorization_temporary", retryable=True)):
            main.run_qianchuan_push(["pending"])
            upload.assert_not_called()
        with self.sessions() as db:
            task = db.get(QianchuanDelivery, "pending")
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.platform_asset_id, "existing-video")
            self.assertEqual(task.upload_task_id, "original-upload")


if __name__ == "__main__":
    unittest.main()
