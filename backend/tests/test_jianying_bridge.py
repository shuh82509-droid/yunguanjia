import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException, Request

os.environ["DATABASE_URL"] = "sqlite:///./jianying-bridge-test.db"

from app.database import Base, SessionLocal, engine
from app.jianying_service import claim_pairing, create_pairing, device_from_request
from app.main import (
    _central_auth_exempt,
    jianying_import_create,
    jianying_import_progress,
    jianying_import_resolve,
    jianying_import_status,
)
from app.models import Asset, AssetFavorite, JianyingDevice, JianyingImportTicket, JianyingPairing
from app.schemas import JianyingImportProgress


def request(path: str = "/", authorization: str = "") -> Request:
    headers = [(b"host", b"app.example.test"), (b"x-forwarded-proto", b"https"), (b"x-forwarded-prefix", b"/wis")]
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    return Request({"type": "http", "method": "GET", "scheme": "https", "path": path, "headers": headers, "server": ("app.example.test", 443), "query_string": b""})


class JianyingBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def setUp(self):
        with SessionLocal() as db:
            db.query(JianyingImportTicket).delete()
            db.query(AssetFavorite).delete()
            db.query(JianyingDevice).delete()
            db.query(JianyingPairing).delete()
            db.query(Asset).delete()
            db.commit()

    def test_pairing_is_single_use_and_token_authenticates_owner(self):
        with SessionLocal() as db:
            pairing = create_pairing(db, {"number": "FD-026222", "realName": "测试用户"}, "https://app.example.test/wis/")
            self.assertEqual(10, len(pairing["code"]))
            self.assertIn("wis-jianying://pair?", pairing["scheme_url"])

            claimed = claim_pairing(db, pairing["code"], "测试电脑")
            self.assertTrue(claimed["access_token"].startswith("wjy_"))
            self.assertEqual("FD-026222", claimed["owner"]["number"])

            with self.assertRaises(HTTPException) as reused:
                claim_pairing(db, pairing["code"], "另一台电脑")
            self.assertEqual(404, reused.exception.status_code)

            authenticated = device_from_request(request(authorization=f"Bearer {claimed['access_token']}"), db)
            self.assertEqual("FD-026222", authenticated.owner_number)

    def test_desktop_auth_routes_bypass_oa_gate_but_browser_routes_do_not(self):
        self.assertTrue(_central_auth_exempt("/api/jianying/pairings/claim"))
        self.assertTrue(_central_auth_exempt("/api/jianying/device/status"))
        self.assertTrue(_central_auth_exempt("/api/jianying/device/uploads/presign"))
        self.assertTrue(_central_auth_exempt("/api/jianying/device/imports/ticket-1"))

        # Creating pairing codes, listing devices and starting a browser-side
        # import still require the signed-in OA user and module permission.
        self.assertFalse(_central_auth_exempt("/api/jianying/pairings"))
        self.assertFalse(_central_auth_exempt("/api/jianying/devices"))
        self.assertFalse(_central_auth_exempt("/api/assets/1/jianying-import"))

    def test_expired_pairing_cannot_be_claimed(self):
        with SessionLocal() as db:
            pairing = create_pairing(db, {"number": "FD-1", "realName": "测试"}, "https://app.example.test/")
            row = db.get(JianyingPairing, pairing["id"])
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
            with self.assertRaises(HTTPException) as expired:
                claim_pairing(db, pairing["code"], "电脑")
            self.assertEqual(410, expired.exception.status_code)

    def test_only_favoriting_owner_device_can_resolve_original(self):
        with SessionLocal() as db:
            asset = Asset(object_key="videos/example.mp4", filename="example.mp4", media_type="video", size=123)
            db.add(asset)
            db.flush()
            db.add(AssetFavorite(user_number="FD-026222", asset_id=asset.id))
            db.commit()
            with patch("app.main.oss_service.url_for", return_value="https://oss.example.test/original.mp4"):
                created = jianying_import_create(asset.id, request(), db, {"number": "FD-026222", "realName": "测试用户"})
                owner_device = JianyingDevice(
                    id="device-owner",
                    owner_number="FD-026222",
                    owner_name="测试用户",
                    device_name="电脑",
                    token_hash="a" * 64,
                    active=True,
                )
                other_device = JianyingDevice(
                    id="device-other",
                    owner_number="FD-999999",
                    owner_name="其他人",
                    device_name="其他电脑",
                    token_hash="b" * 64,
                    active=True,
                )
                db.add_all([owner_device, other_device])
                db.commit()
                resolved = jianying_import_resolve(created["ticket_id"], db, owner_device)
                self.assertEqual("https://oss.example.test/original.mp4", resolved["download_url"])
                claimed = jianying_import_status(
                    created["ticket_id"], db, {"number": "FD-026222", "realName": "测试用户"}
                )
                self.assertEqual("claimed", claimed["status"])
                self.assertEqual(5, claimed["progress"])

                updated = jianying_import_progress(
                    created["ticket_id"],
                    JianyingImportProgress(
                        status="downloading",
                        progress=42,
                        message="正在下载原视频 · 55%",
                        downloaded_bytes=68,
                        total_bytes=123,
                        speed_bps=34,
                        eta_seconds=2,
                        helper_version="1.2.0",
                    ),
                    db,
                    owner_device,
                )
                self.assertEqual("downloading", updated["status"])
                self.assertEqual(42, updated["progress"])
                self.assertEqual(68, updated["downloaded_bytes"])
                self.assertEqual(123, updated["total_bytes"])
                self.assertEqual(34, updated["speed_bps"])
                self.assertEqual(2, updated["eta_seconds"])
                self.assertEqual("1.2.0", updated["helper_version"])

                completed = jianying_import_progress(
                    created["ticket_id"],
                    JianyingImportProgress(status="completed", progress=98, message="已成功导入剪映素材区"),
                    db,
                    owner_device,
                )
                self.assertEqual("completed", completed["status"])
                self.assertEqual(100, completed["progress"])
                ignored = jianying_import_progress(
                    created["ticket_id"],
                    JianyingImportProgress(status="importing", progress=90, message="迟到的进度"),
                    db,
                    owner_device,
                )
                self.assertEqual("completed", ignored["status"])
                self.assertEqual(100, ignored["progress"])
                with self.assertRaises(HTTPException) as forbidden:
                    jianying_import_resolve(created["ticket_id"], db, other_device)
                self.assertEqual(404, forbidden.exception.status_code)

                with self.assertRaises(HTTPException) as hidden:
                    jianying_import_status(
                        created["ticket_id"], db, {"number": "FD-999999", "realName": "其他人"}
                    )
                self.assertEqual(404, hidden.exception.status_code)


if __name__ == "__main__":
    unittest.main()
