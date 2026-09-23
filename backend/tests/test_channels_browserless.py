"""Offline protocol and workflow tests: no real upload, login, or publication."""
import builtins
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.channels_credentials import encrypt_credentials, decrypt_credentials, ChannelsCredentialsError, normalize_cookies, public_channels_message
from app.channels_internal_api import HttpJsonClient, BrowserJsonClient, ChannelsInternalPublisher, ChannelsInternalApiError, ChannelsPublishUncertain
from app.channels_service import ChannelsService, ChannelsError
from app.config import settings
from app.database import Base
from app.models import Asset, ChannelsAccount, ChannelsDelivery
from app import main


def cookie(value="test-session", **extra):
    return {"name": "sessionid", "value": value, "domain": ".weixin.qq.com", "path": "/", "secure": True,
            "expires": -1, "httpOnly": True, "sameSite": "Lax", **extra}


def bundle():
    return {"version": 1, "cookies": [cookie()], "user_agent": "Captured-Account-Browser/1.0"}


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CHANNELS_SESSION_FERNET_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_roundtrip_encrypted_bundle_and_session_with_durable_key(self):
        encrypted, session = encrypt_credentials(self.root, [cookie()], "captured-UA")
        self.assertNotIn("test-session", encrypted + session)
        self.assertEqual(decrypt_credentials(self.root, encrypted)["cookies"], [cookie()])
        key = (self.root / ".session-fernet.key").read_bytes()
        self.assertEqual(Fernet(key).decrypt(session.encode()), b"test-session")
        encrypt_credentials(self.root, [cookie("second")], "captured-UA")
        self.assertEqual(key, (self.root / ".session-fernet.key").read_bytes())

    def test_missing_key_does_not_silently_generate_a_new_key(self):
        encrypted, _ = encrypt_credentials(self.root, [cookie()], "UA")
        (self.root / ".session-fernet.key").unlink()
        with self.assertRaises(ChannelsCredentialsError):
            decrypt_credentials(self.root, encrypted)
        self.assertFalse((self.root / ".session-fernet.key").exists())

    def test_tampered_or_wrong_key_requires_rescan_without_secret_leak(self):
        encrypted, _ = encrypt_credentials(self.root, [cookie()], "UA")
        with patch.dict(os.environ, {"CHANNELS_SESSION_FERNET_KEY": Fernet.generate_key().decode()}):
            with self.assertRaises(ChannelsCredentialsError) as caught:
                decrypt_credentials(self.root, encrypted)
        self.assertNotIn(encrypted, str(caught.exception))

    def test_cookie_allowlist_rejects_suffix_tricks_and_missing_domain(self):
        values = [cookie(domain=d) for d in ["weixin.qq.com", ".channels.weixin.qq.com", "weixin.qq.com.evil.test", "evilweixin.qq.com", "qq.com", ""]]
        self.assertEqual(len(normalize_cookies(values)), 2)

    def test_missing_session_or_user_agent_cannot_be_saved(self):
        for cookies, ua in [([cookie(name="other")], "UA"), ([cookie()], "")]:
            with self.assertRaises(ChannelsCredentialsError):
                encrypt_credentials(self.root, cookies, ua)

    def test_legacy_diagnostics_are_hidden_without_losing_reason(self):
        message = "已点击发布，但未确认；平台回执：{token=old-secret} request={sessionid=old-secret}"
        safe = public_channels_message(message)
        self.assertIn("已点击发布，但未确认", safe)
        self.assertNotIn("old-secret", safe)
        self.assertIn("已隐藏", safe)
        self.assertNotIn("old-secret", public_channels_message("network url?token=old-secret"))

    def test_delivery_output_hides_old_secrets_without_mutating_history(self):
        message = "未读到平台确认；平台回执：{token=old-secret}"
        task = ChannelsDelivery(id="test", batch_id="test", asset_id=1, account_id="test", created_by_number="test",
            message=message, error_message=message, metrics_message=message, status="failed", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        output = main.channels_delivery_out(task)
        self.assertNotIn("old-secret", json.dumps(output))
        self.assertEqual(task.message, message)


class HttpClientTests(unittest.TestCase):
    def client(self, handler, **kwargs):
        client = HttpJsonClient(bundle(), transport=httpx.MockTransport(handler), **kwargs)
        self.addCleanup(client.close)
        return client

    def test_scoped_cookies_actual_user_agent_and_nonspoofed_headers(self):
        seen = []
        client = self.client(lambda req: seen.append(req) or httpx.Response(200, json={"errCode": 0}))
        client.call("/auth/auth_data", {"scene": 7})
        self.assertIn("sessionid=test-session", seen[0].headers["cookie"])
        self.assertEqual(seen[0].headers["user-agent"], bundle()["user_agent"])
        self.assertEqual(seen[0].headers["origin"], "https://channels.weixin.qq.com")
        self.assertNotIn("finger-print-device-id", seen[0].headers)
        self.assertNotIn("sec-fetch-site", seen[0].headers)
        self.assertNotIn("sec-ch-ua", seen[0].headers)
        self.assertEqual(client.client.cookies.jar._cookies[".weixin.qq.com"]["/"]["sessionid"].secure, True)
        self.assertEqual(client.client.build_request("GET", "https://example.com/").headers.get("cookie"), None)
        self.assertEqual(client.client.build_request("GET", "http://channels.weixin.qq.com/").headers.get("cookie"), None)

    def test_rotated_set_cookie_sent_on_next_request_and_saved(self):
        seen, persisted = [], []
        def handler(req):
            seen.append(req)
            return httpx.Response(200, json={"errCode": 0}, headers={"set-cookie": "sessionid=rotated; Domain=.weixin.qq.com; Path=/; Secure; HttpOnly"})
        client = self.client(handler, on_cookies=persisted.append)
        client.call("/auth/auth_data", {})
        client.call("/auth/auth_data", {})
        self.assertIn("sessionid=rotated", seen[1].headers["cookie"])
        self.assertEqual(persisted[-1][0]["value"], "rotated")

    def test_rotation_save_error_cannot_discard_publication_ack(self):
        client = self.client(lambda req: httpx.Response(200, json={"errCode": 0}), on_cookies=MagicMock(side_effect=RuntimeError("disk")))
        result = client.call("/post/post_create", {}, side_effect=True)
        self.assertEqual(result["errCode"], 0)

    def test_redirect_is_not_followed_and_requires_authorization(self):
        seen = []
        client = self.client(lambda req: seen.append(req) or httpx.Response(302, headers={"location": "https://outside.example/"}))
        with self.assertRaises(ChannelsInternalApiError) as caught:
            client.call("/auth/auth_data", {})
        self.assertEqual(caught.exception.code, "AUTH_EXPIRED")
        self.assertEqual(len(seen), 1)

    def test_auth_error_family_including_nested_base_response(self):
        for code in [300330, 300331, 300350]:
            client = self.client(lambda req, c=code: httpx.Response(200, json={"errCode": 0, "data": {"baseResp": {"ret": c}}}))
            with self.assertRaises(ChannelsInternalApiError) as caught:
                client.call("/auth/auth_data", {})
            self.assertEqual(caught.exception.code, "AUTH_EXPIRED")

    def test_final_network_timeout_five_hundred_html_or_unknown_json_never_retry(self):
        for result in [httpx.ReadTimeout("secret"), httpx.ConnectError("secret"), httpx.Response(503), httpx.Response(200, text="login"), httpx.Response(200, json={})]:
            calls = []
            def handler(req):
                calls.append(req)
                if isinstance(result, Exception):
                    raise result
                return result
            client = self.client(handler)
            with self.assertRaises(ChannelsPublishUncertain) as caught:
                client.call("/post/post_create", {}, side_effect=True)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("secret", str(caught.exception))

    def test_structured_publish_rejection_is_definitive_but_does_not_expose_message(self):
        client = self.client(lambda req: httpx.Response(200, json={"errCode": 0, "data": {"baseResp": {"ret": 9001, "errMsg": "sessionid=secret"}}}))
        with self.assertRaises(ChannelsInternalApiError) as caught:
            client.call("/post/post_create", {}, side_effect=True)
        self.assertEqual(caught.exception.code, "PUBLISH_REJECTED")
        self.assertTrue(caught.exception.definitive)
        self.assertNotIn("secret", str(caught.exception))

    def test_browser_and_http_parser_agree(self):
        data = {"errCode": 0, "baseResp": {"ret": 300332}}
        page = MagicMock()
        page.evaluate.return_value = {"httpStatus": 200, "json": True, "data": data}
        for client in [BrowserJsonClient(page), self.client(lambda req: httpx.Response(200, json=data))]:
            with self.assertRaises(ChannelsInternalApiError) as caught:
                client.call("/auth/auth_data", {})
            self.assertEqual(caught.exception.code, "AUTH_EXPIRED")

    def test_invalid_paths_do_not_make_requests(self):
        handler = MagicMock()
        client = self.client(handler)
        for path in ["//outside", "https://outside", "/../outside", "/auth?bad=1"]:
            with self.assertRaises(ChannelsInternalApiError):
                client.call(path, {})
        handler.assert_not_called()


class BrowserlessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ChannelsService()
        self.service.auth_root = Path(self.temp.name)
        self.settings = SimpleNamespace(**vars(settings))
        self.settings.channels_publish_transport = "direct_internal_api"
        self.settings.channels_direct_account_ids = ""
        self.settings.channels_internal_api_browser_fallback = True
        for p in [patch("app.channels_service.SessionLocal", self.Session), patch("app.main.SessionLocal", self.Session),
                  patch.dict(os.environ, {"CHANNELS_SESSION_FERNET_KEY": ""}),
                  patch("app.channels_service.settings", self.settings)]:
            p.start()
            self.addCleanup(p.stop)
        encrypted, session = encrypt_credentials(self.service.auth_root, [cookie()], bundle()["user_agent"])
        self.account = ChannelsAccount(id="a", owner_number="FD-test", nickname="test", external_account_id="finder-1",
            auth_file=str(self.service.auth_root / "a.json"), status="active", channel_cookies_ciphertext=encrypted, session_cookie_ciphertext=session)
        with self.Session() as db:
            db.add(self.account)
            db.commit()
        self.kwargs = dict(account=self.account, object_key="test.mp4", filename="test.mp4", title="test", description="", tags=[], mark_submitted=MagicMock())

    def mock_transport(self, handler):
        real = HttpJsonClient
        p = patch("app.channels_service.HttpJsonClient", side_effect=lambda b, **kw: real(b, **kw, transport=httpx.MockTransport(handler)))
        p.start()
        self.addCleanup(p.stop)

    def test_direct_success_never_imports_playwright_and_checks_identity_before_download(self):
        calls = []
        def handler(req):
            calls.append(req.url.path)
            if req.url.path.endswith("/auth/auth_data"):
                return httpx.Response(200, json={"errCode": 0, "data": {"finderUser": {"finderUsername": "finder-1"}}})
            if req.url.path.endswith("/helper/helper_upload_params"):
                return httpx.Response(200, json={"errCode": 0, "data": {"authKey": "secret", "uin": "1", "videoFileType": 1, "pictureFileType": 2}})
            return httpx.Response(200, json={"errCode": 0, "data": {"traceKey": "trace"}})
        self.mock_transport(handler)
        original_import = builtins.__import__
        def forbid(name, *args, **kwargs):
            if name.startswith("playwright"):
                raise AssertionError("direct path imported browser")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=forbid), patch.object(self.service, "_publish_prepared", return_value={"accepted": True, "confirmed": False}) as prepared:
            result = self.service.publish(**self.kwargs)
        self.assertEqual(result["publish_transport"], "direct_internal_api")
        self.assertFalse(result["confirmed"])
        self.assertTrue(calls[0].endswith("/auth/auth_data"))
        prepared.assert_called_once()

    def test_identity_mismatch_does_not_download_or_fallback(self):
        self.mock_transport(lambda req: httpx.Response(200, json={"errCode": 0, "data": {"finderUser": {"finderUsername": "wrong"}}}))
        with patch.object(self.service, "_publish_prepared") as prepared, patch.object(self.service, "_publish_with_internal_api") as browser:
            with self.assertRaises(ChannelsError) as caught:
                self.service.publish(**self.kwargs)
        self.assertEqual(caught.exception.code, "ACCOUNT_ID_MISMATCH")
        prepared.assert_not_called()
        browser.assert_not_called()

    def test_missing_cookie_fallback_and_strict_mode(self):
        with self.Session() as db:
            db.get(ChannelsAccount, "a").channel_cookies_ciphertext = ""
            db.commit()
        with patch.object(self.service, "_publish_with_internal_api", return_value={"accepted": True}) as browser:
            self.assertEqual(self.service.publish(**self.kwargs)["publish_transport"], "internal_api")
            browser.assert_called_once()
            browser.reset_mock()
            with patch.object(self.settings, "channels_internal_api_browser_fallback", False), self.assertRaises(ChannelsError):
                self.service.publish(**self.kwargs)
            browser.assert_not_called()

    def test_unsafe_errors_never_fallback(self):
        for code, stage in [("AUTH_EXPIRED", "authorization"), ("ACCOUNT_ID_MISMATCH", "internal_preflight"),
                            ("PUBLISH_REJECTED", "submit_publish"), ("API_UNAVAILABLE", "internal_preflight")]:
            with patch.object(self.service, "_publish_with_direct_internal_api", side_effect=ChannelsInternalApiError("stopped", stage, code=code)), patch.object(self.service, "_publish_with_internal_api") as browser:
                with self.assertRaises(ChannelsError):
                    self.service.publish(**self.kwargs)
                browser.assert_not_called()

    def test_self_shot_can_fallback_to_existing_ui_only_before_submission(self):
        error = ChannelsInternalApiError("self shot requires browser", code="USE_BROWSER_FOR_SELF_SHOT")
        with patch.object(self.service, "_publish_with_direct_internal_api", side_effect=error), patch.object(self.service, "_publish_with_internal_api", side_effect=error), patch.object(self.service, "_publish_with_browser_ui", return_value={"accepted": True}) as ui:
            self.assertEqual(self.service.publish(**self.kwargs)["publish_transport"], "browser_ui")
            ui.assert_called_once()

    def test_checkpoint_prevents_fallback_even_if_error_wrongly_says_compatible(self):
        def run(**kwargs):
            kwargs["mark_submitted"]("unique")
            raise ChannelsInternalApiError("unsupported", code="API_UNSUPPORTED")
        with patch.object(self.service, "_publish_with_direct_internal_api", side_effect=run), patch.object(self.service, "_publish_with_internal_api") as browser:
            with self.assertRaises(ChannelsError):
                self.service.publish(**self.kwargs)
            browser.assert_not_called()
        self.kwargs["mark_submitted"].assert_called_once_with("unique")

    def test_reauthorization_upserts_by_owner_and_stable_id(self):
        args = ({"owner_number": "FD-test", "owner_name": "test"}, "new-id", self.service.auth_root / "new.json", "new nickname", "", "finder-1", "cipher", "session-cipher")
        saved, _ = self.service._save_authorization(*args)
        self.assertEqual(saved, "a")
        with self.Session() as db:
            self.assertEqual(len(db.scalars(select(ChannelsAccount)).all()), 1)
            self.assertEqual(db.get(ChannelsAccount, "a").nickname, "new nickname")
        other = list(args)
        other[0] = {"owner_number": "FD-other", "owner_name": "other"}
        saved, _ = self.service._save_authorization(*other)
        self.assertEqual(saved, "new-id")

    def test_same_nickname_different_external_account_is_not_rebound(self):
        with self.Session() as db:
            db.add(ChannelsAccount(id="wrong", owner_number="FD-test", nickname="test", external_account_id="finder-other", status="expired"))
            task = ChannelsDelivery(id="old", batch_id="batch", asset_id=1, account_id="wrong", created_by_number="FD-test", status="failed", failure_stage="authorization")
            db.add(task)
            db.commit()
            self.assertEqual(self.service._resume_tasks_after_authorization(db, db.get(ChannelsAccount, "a")), 0)
            self.assertEqual(task.account_id, "wrong")

    def seed_task(self):
        with self.Session() as db:
            asset = Asset(object_key="video.mp4", filename="video.mp4", media_type="video", size=1)
            db.add(asset)
            db.flush()
            db.add(ChannelsDelivery(id="task", batch_id="batch", asset_id=asset.id, account_id="a", created_by_number="FD-test", status="pending", title="test"))
            db.commit()

    def test_uncertain_submit_stays_readback_only_even_after_worker_restart(self):
        self.seed_task()
        def publish(**kwargs):
            kwargs["mark_submitted"]("client-one")
            raise ChannelsError("uncertain", "confirm_publish", code="PUBLISH_RESULT_UNCERTAIN", definitive=False, transport="direct_internal_api")
        with patch("app.main.channels_service.publish", side_effect=publish) as send, patch("app.main._require_assets_review_approved"):
            main.run_channels_push("task")
            main.run_channels_push("task")
            send.assert_called_once()
        with self.Session() as db:
            row = db.get(ChannelsDelivery, "task")
            self.assertEqual(row.status, "submitted")
            self.assertEqual(row.publish_transport, "direct_internal_api")
            self.assertIsNotNone(row.publish_clicked_at)

    def test_expired_authorization_marks_account_expired_without_auto_retry(self):
        self.seed_task()
        with patch("app.main.channels_service.publish", side_effect=ChannelsError("please scan", "authorization", code="AUTH_EXPIRED")), patch("app.main._require_assets_review_approved"):
            main.run_channels_push("task")
        with self.Session() as db:
            self.assertEqual(db.get(ChannelsAccount, "a").status, "expired")
            self.assertEqual(db.get(ChannelsDelivery, "task").status, "failed")
            self.assertEqual(db.get(ChannelsDelivery, "task").failure_stage, "authorization")

    def test_pending_task_that_lost_human_approval_cannot_start(self):
        self.seed_task()
        with patch("app.main._require_assets_review_approved", side_effect=main.HTTPException(409, "审核未通过")), patch("app.main.channels_service.publish") as send:
            main.run_channels_push("task")
            send.assert_not_called()
        with self.Session() as db:
            self.assertEqual(db.get(ChannelsDelivery, "task").failure_stage, "review_required")

    def test_human_gate_is_rechecked_immediately_before_checkpoint(self):
        self.seed_task()
        sent = []
        def publish(**kwargs):
            kwargs["mark_submitted"]("client")
            sent.append(True)
            return {"accepted": True}
        with patch("app.main._require_assets_review_approved", side_effect=[None, main.HTTPException(409, "approval changed")]), patch("app.main.channels_service.publish", side_effect=publish):
            main.run_channels_push("task")
        self.assertEqual(sent, [])
        with self.Session() as db:
            row = db.get(ChannelsDelivery, "task")
            self.assertIsNone(row.publish_clicked_at)
            self.assertEqual(row.failure_stage, "review_required")

    def test_acknowledged_is_not_displayed_as_published(self):
        self.seed_task()
        with patch("app.main._require_assets_review_approved"), patch("app.main.channels_service.publish", return_value={"accepted": True, "confirmed": False, "publish_transport": "direct_internal_api"}):
            main.run_channels_push("task")
        with self.Session() as db:
            self.assertEqual(db.get(ChannelsDelivery, "task").status, "submitted")

    def test_api_never_serializes_credential_columns(self):
        with self.Session() as db:
            output = main.channels_accounts(db=db, user={"employee_number": "FD-test", "number": "FD-test"})
        serialized = json.dumps(output)
        self.assertEqual(output["total"], 1)
        self.assertNotIn("ciphertext", serialized)
        self.assertNotIn("test-session", serialized)

    def test_complete_http_publish_with_mock_cdn_is_single_submit_after_checkpoint(self):
        events = []
        def handler(req):
            path = req.url.path
            if path.endswith("/auth/auth_data"):
                data = {"finderUser": {"finderUsername": "finder-1"}}
            elif path.endswith("/helper/helper_upload_params"):
                data = {"authKey": "secret", "uin": "1", "videoFileType": 1, "pictureFileType": 2}
            elif path.endswith("/post/get-finder-post-trace-key"):
                data = {"traceKey": "trace"}
            elif path.endswith("/post/post_clip_video"):
                data = {"draftId": "draft"}
            elif path.endswith("/post/post_clip_video_result"):
                data = {"flag": 1, "url": "https://finder.video.qq.com/final.mp4", "width": 1080, "height": 1920}
            elif path.endswith("/post/post_create"):
                events.append(("post", json.loads(req.content)["clientid"]))
                data = {}
            else:
                raise AssertionError(path)
            return httpx.Response(200, json={"errCode": 0, "data": data})
        self.mock_transport(handler)
        uploader = MagicMock()
        uploader.upload.return_value = {"url": "https://finder.video.qq.com/file", "file_size": 1, "task_id": "task", "md5sum": "md5"}
        self.kwargs.update(cover_object_key="cover.jpg", cover_filename="cover.jpg", mark_submitted=lambda x: events.append(("checkpoint", x)))
        def download(url, target, *args, **kwargs):
            target.write_bytes(b"original")
        with patch.object(self.service, "_download_with_resume", side_effect=download), patch("app.channels_service.oss_service.url_for", return_value="https://test.invalid/file"), patch("app.channels_service.probe_video", return_value={"duration": 1}), patch("app.channels_internal_api.probe_video", return_value={"duration": 1, "width": 1080, "height": 1920, "file_size": 8}), patch("app.channels_internal_api.ChannelsCdnUploader", return_value=uploader), patch.object(self.service, "_publish_with_internal_api", side_effect=AssertionError("browser fallback forbidden")), patch.object(self.service, "_launch_account_session", side_effect=AssertionError("browser forbidden")):
            with patch("app.channels_internal_api.prepare_custom_cover", return_value=(Path("full.jpg"), Path("profile.jpg"))), patch("app.channels_internal_api.verify_uploaded_cover", return_value=True):
                result = self.service.publish(**self.kwargs)
        self.assertEqual([e[0] for e in events], ["checkpoint", "post"])
        self.assertEqual(events[0][1], events[1][1])
        self.assertEqual(result["publish_transport"], "direct_internal_api")
        self.assertTrue(result["accepted"])
        self.assertFalse(result["confirmed"])
        self.assertEqual(uploader.upload.call_count, 3)
        uploader.session.close.assert_called_once()

    def test_rotation_after_revoke_cannot_resurrect_credentials(self):
        def handler(req):
            with self.Session() as db:
                row = db.get(ChannelsAccount, "a")
                row.status = "revoked"
                row.channel_cookies_ciphertext = row.session_cookie_ciphertext = ""
                db.commit()
            return httpx.Response(200, json={"errCode": 300330}, headers={"set-cookie": "sessionid=rotated; Domain=.weixin.qq.com; Path=/; Secure"})
        self.mock_transport(handler)
        with self.assertRaises(ChannelsError):
            self.service.publish(**self.kwargs)
        with self.Session() as db:
            row = db.get(ChannelsAccount, "a")
            self.assertEqual(row.status, "revoked")
            self.assertEqual(row.channel_cookies_ciphertext, "")

    def test_revoke_clears_both_encrypted_credentials(self):
        with self.Session() as db, patch("app.main.channels_promotion_service.revoke_authorization"), patch("app.main.channels_service.remove_authorization_files"):
            main.channels_account_delete("a", db=db, user={"number": "FD-test"})
            row = db.get(ChannelsAccount, "a")
            self.assertEqual(row.status, "revoked")
            self.assertEqual(row.channel_cookies_ciphertext, "")
            self.assertEqual(row.session_cookie_ciphertext, "")

    def test_checkpointed_auth_failure_is_not_requeued_after_rescan(self):
        self.seed_task()
        with self.Session() as db:
            row = db.get(ChannelsDelivery, "task")
            row.status = "failed"
            row.failure_stage = "authorization"
            row.publish_clicked_at = datetime.utcnow()
            self.service._resume_tasks_after_authorization(db, db.get(ChannelsAccount, "a"))
            self.assertEqual(row.status, "failed")

    def test_qr_verification_reopens_same_profile_and_rejects_changed_identity(self):
        context = MagicMock()
        context.cookies.return_value = [cookie()]
        context.new_page.return_value.evaluate.return_value = "Captured-UA"
        auth_file = self.service.auth_root / "new.json"
        with patch.object(self.service, "_launch_account_session", return_value=(MagicMock(), context, True)) as launch, patch.object(self.service, "_close_account_session") as close, patch.object(self.service, "_goto_creator_page"), patch.object(self.service, "_authorization_expired", return_value=False), patch("app.channels_service.read_authenticated_identity", side_effect=[{"external_account_id": "finder-1"}, {"external_account_id": "wrong"}]):
            with self.assertRaises(ChannelsError):
                self.service._verify_persistent_authorization(MagicMock(), auth_file, "finder-1")
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(close.call_count, 2)
        self.assertEqual(launch.call_args_list[0].args[1], launch.call_args_list[1].args[1])

    def test_gray_allowlist_does_not_switch_other_accounts(self):
        self.settings.channels_publish_transport = "auto"
        self.settings.channels_direct_account_ids = "another-account"
        with patch.object(self.service, "_publish_with_direct_internal_api") as direct, patch.object(self.service, "_publish_with_internal_api", return_value={"accepted": True}):
            self.assertEqual(self.service.publish(**self.kwargs)["publish_transport"], "internal_api")
            direct.assert_not_called()

    def test_auto_uses_saved_http_credentials_without_starting_browser(self):
        self.settings.channels_publish_transport = "auto"
        with patch.object(self.service, "_publish_with_direct_internal_api", return_value={"accepted": True}) as direct, \
             patch.object(self.service, "_publish_with_internal_api") as browser:
            self.assertEqual(self.service.publish(**self.kwargs)["publish_transport"], "direct_internal_api")
            direct.assert_called_once()
            browser.assert_not_called()

    def test_explicit_browser_and_legacy_account_preserve_operator_choice(self):
        for transport, credentials in (("internal_api", True), ("auto", False)):
            self.settings.channels_publish_transport = transport
            if not credentials:
                self.account.channel_cookies_ciphertext = ""
            with patch.object(self.service, "_publish_with_direct_internal_api") as direct, \
                 patch.object(self.service, "_publish_with_internal_api", return_value={"accepted": True}):
                self.assertEqual(self.service.publish(**self.kwargs)["publish_transport"], "internal_api")
                direct.assert_not_called()

    def test_existing_database_migration_is_additive(self):
        from sqlalchemy import text
        with self.engine.begin() as connection:
            for field in ["channel_cookies_ciphertext", "session_cookie_ciphertext", "cookies_updated_at", "last_verified_at"]:
                connection.execute(text("ALTER TABLE channels_accounts DROP COLUMN " + field))
        with patch("app.main.engine", self.engine):
            main.ensure_asset_schema()
        with self.Session() as db:
            row = db.get(ChannelsAccount, "a")
            self.assertEqual(row.nickname, "test")
            self.assertEqual(row.channel_cookies_ciphertext, "")
