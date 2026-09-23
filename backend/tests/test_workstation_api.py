import os
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["WIS_WORKSTATION_API_TOKEN"] = "test-workstation-token-with-more-than-32-characters"

from app.database import Base, SessionLocal, engine
from app.config import settings
from app.main import (
    ensure_asset_schema,
    workstation_access_authorize,
    workstation_access_authorize_identity,
    WorkstationIdentityAuthorize,
    workstation_asset_detail,
    workstation_assets,
    workstation_return_complete,
    workstation_return_presign,
    workstation_return_status,
    workstation_qianchuan_delivery_retry,
)
from app.models import (
    Asset,
    ModuleAccessGrant,
    OaAccessGrant,
    QianchuanDelivery,
    WorkstationReturn,
)
from app.oss_service import oss_service
from app.schemas import WorkstationReturnComplete, WorkstationReturnCreate
from app.workstation_auth import require_workstation


def request_with_token(token: str = "", oa_token: str = "") -> Request:
    headers = []
    if token:
        headers.append((b"x-wis-workstation-token", token.encode("utf-8")))
    if oa_token:
        headers.append((b"x-oa-token", oa_token.encode("utf-8")))
    return Request({"type": "http", "headers": headers})


class WorkstationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        ensure_asset_schema()

    def setUp(self):
        with SessionLocal() as db:
            db.execute(delete(QianchuanDelivery))
            db.execute(delete(WorkstationReturn))
            db.execute(delete(Asset))
            db.execute(delete(ModuleAccessGrant))
            db.execute(delete(OaAccessGrant))
            db.commit()

    def test_workstation_token_is_required_and_constant_scoped(self):
        with self.assertRaises(HTTPException) as missing:
            require_workstation(request_with_token())
        self.assertEqual(missing.exception.status_code, 401)

        service = require_workstation(
            request_with_token("test-workstation-token-with-more-than-32-characters")
        )
        self.assertEqual(service["number"], "SERVICE-WIS-REMIX")
        self.assertTrue(service["service"])

    def test_workstation_access_authority_uses_live_database_grants(self):
        with SessionLocal() as db:
            db.add(
                OaAccessGrant(
                    identifier="name:杨雯",
                    identifier_type="name",
                    real_name="杨雯",
                    active=True,
                    source="admin",
                )
            )
            db.commit()

        delegated_user = {
            "number": "FD-YANGWEN",
            "realName": "杨雯",
            "groupName": "品牌创意中心",
            "status": "normal",
        }
        with patch("app.main.user_from_token", return_value=delegated_user):
            result = workstation_access_authorize(
                request=request_with_token(
                    "test-workstation-token-with-more-than-32-characters",
                    "delegated-oa-token",
                ),
                _service={},
            )

        self.assertTrue(result["allowed"])
        self.assertEqual(result["authority"], "wis-video-center")
        self.assertEqual(result["module"], "material-workbench")
        self.assertEqual(result["user"]["realName"], "杨雯")

    def test_workstation_access_authority_enforces_selected_module_scope(self):
        with SessionLocal() as db:
            db.add(
                OaAccessGrant(
                    identifier="name:模块权限同事",
                    identifier_type="name",
                    real_name="模块权限同事",
                    active=True,
                    source="admin",
                )
            )
            db.add(
                ModuleAccessGrant(
                    identifier="name:模块权限同事",
                    identifier_type="name",
                    real_name="模块权限同事",
                    access_mode="selected",
                    modules=["data-dashboard"],
                )
            )
            db.commit()

        delegated_user = {
            "number": "FD-MODULE-SCOPE",
            "realName": "模块权限同事",
            "groupName": "品牌创意中心",
            "status": "normal",
        }
        with patch("app.main.user_from_token", return_value=delegated_user):
            with self.assertRaises(HTTPException) as denied:
                workstation_access_authorize(
                    request=request_with_token(
                        "test-workstation-token-with-more-than-32-characters",
                        "delegated-oa-token",
                    ),
                    _service={},
                )
            allowed = workstation_access_authorize(
                request=request_with_token(
                    "test-workstation-token-with-more-than-32-characters",
                    "delegated-oa-token",
                ),
                module="data-dashboard",
                _service={},
            )

        self.assertEqual(denied.exception.status_code, 403)
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["module"], "data-dashboard")

    def test_workstation_access_authority_requires_oa_token(self):
        with self.assertRaises(HTTPException) as missing:
            workstation_access_authorize(
                request=request_with_token(
                    "test-workstation-token-with-more-than-32-characters"
                ),
                _service={},
            )

        self.assertEqual(missing.exception.status_code, 401)

    def test_workstation_access_authority_applies_revocation_immediately(self):
        with SessionLocal() as db:
            db.add(
                OaAccessGrant(
                    identifier="name:已撤权同事",
                    identifier_type="name",
                    real_name="已撤权同事",
                    active=False,
                    source="admin",
                )
            )
            db.commit()

        revoked_user = {
            "number": "FD-REVOKED",
            "realName": "已撤权同事",
            "groupName": "品牌创意中心",
            "status": "normal",
        }
        with patch("app.main.user_from_token", return_value=revoked_user):
            with self.assertRaises(HTTPException) as denied:
                workstation_access_authorize(
                    request=request_with_token(
                        "test-workstation-token-with-more-than-32-characters",
                        "revoked-oa-token",
                    ),
                    _service={},
                )

        self.assertEqual(denied.exception.status_code, 403)

    def test_verified_workstation_identity_rechecks_login_and_module_scope(self):
        with SessionLocal() as db:
            db.add(
                OaAccessGrant(
                    identifier="name:一创测试同事",
                    identifier_type="name",
                    real_name="一创测试同事",
                    active=True,
                    source="admin",
                )
            )
            db.add(
                ModuleAccessGrant(
                    identifier="name:一创测试同事",
                    identifier_type="name",
                    real_name="一创测试同事",
                    access_mode="selected",
                    modules=["ai-first-creation"],
                )
            )
            db.commit()

        result = workstation_access_authorize_identity(
            payload=WorkstationIdentityAuthorize(
                number="FD-FIRST-CREATION",
                name="一创测试同事",
                lark_user_id="ou_first_creation",
            ),
            request=request_with_token(
                "test-workstation-token-with-more-than-32-characters"
            ),
            module="ai-first-creation",
            _service={},
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["module"], "ai-first-creation")

        with self.assertRaises(HTTPException) as denied:
            workstation_access_authorize_identity(
                payload=WorkstationIdentityAuthorize(
                    number="FD-FIRST-CREATION",
                    name="一创测试同事",
                ),
                request=request_with_token(
                    "test-workstation-token-with-more-than-32-characters"
                ),
                module="cloud-manager",
                _service={},
            )
        self.assertEqual(denied.exception.status_code, 403)

    def test_workstation_access_authority_keeps_department_policy(self):
        department_user = {
            "number": "FD-BRAND",
            "realName": "品牌营销同事",
            "groupName": "品牌营销部",
            "status": "normal",
        }
        with patch("app.main.user_from_token", return_value=department_user):
            result = workstation_access_authorize(
                request=request_with_token(
                    "test-workstation-token-with-more-than-32-characters",
                    "department-oa-token",
                ),
                _service={},
            )

        self.assertTrue(result["allowed"])

    def test_workstation_asset_list_filters_library_type_and_returns_covers(self):
        with SessionLocal() as db:
            db.add_all(
                [
                    Asset(
                        object_key="yxb/uploads/a/source.mp4",
                        filename="源视频.mp4",
                        media_type="video",
                        size=1024,
                        modified_at=datetime.utcnow(),
                        category="水润面膜",
                        content_type="口播",
                        status="待整理",
                        asset_scope="marketing_video",
                        library_type="source",
                        asset_subtype="达人/KOC原片",
                        tags=["有效素材"],
                        cover_url="https://cover/source.jpg",
                    ),
                    Asset(
                        object_key="yxb/uploads/a/remix.mp4",
                        filename="成片.mp4",
                        media_type="video",
                        size=1024,
                        modified_at=datetime.utcnow(),
                        category="水润面膜",
                        content_type="其他",
                        status="待整理",
                        asset_scope="marketing_video",
                        library_type="remix",
                        asset_subtype="AI混剪成片",
                        tags=[],
                        cover_url="https://cover/remix.jpg",
                    ),
                ]
            )
            db.commit()
            with patch.object(oss_service, "url_for", side_effect=lambda key, **_: f"https://oss/{key}"):
                source_page = workstation_assets(
                    q="",
                    category="",
                    folder_name="",
                    library_type="source",
                    page=1,
                    page_size=1,
                    db=db,
                    _service={},
                )
                remix_page = workstation_assets(
                    q="",
                    category="",
                    folder_name="",
                    library_type="remix",
                    page=1,
                    page_size=1,
                    db=db,
                    _service={},
                )
                all_page = workstation_assets(
                    q="",
                    category="",
                    folder_name="",
                    library_type="all",
                    page=1,
                    page_size=10,
                    db=db,
                    _service={},
                )
                remix_detail = workstation_asset_detail(
                    remix_page["items"][0]["id"], db, {}
                )
        self.assertEqual(source_page["total"], 1)
        self.assertEqual(source_page["items"][0]["library_type"], "source")
        self.assertEqual(source_page["items"][0]["cover_url"], "https://cover/source.jpg")
        self.assertEqual(remix_page["total"], 1)
        self.assertEqual(remix_detail["filename"], "成片.mp4")
        self.assertEqual(remix_detail["library_type"], "remix")
        self.assertEqual(remix_detail["cover_url"], "https://cover/remix.jpg")
        self.assertEqual(all_page["total"], 2)
        self.assertEqual(all_page["library_type"], "all")

    def test_workstation_asset_list_rejects_invalid_library_type(self):
        with SessionLocal() as db, self.assertRaises(HTTPException) as invalid:
            workstation_assets(
                q="",
                category="",
                folder_name="",
                library_type="unknown",
                page=1,
                page_size=20,
                db=db,
                _service={},
            )
        self.assertEqual(invalid.exception.status_code, 422)

    def test_workstation_return_is_idempotent_and_persists_provenance(self):
        payload = WorkstationReturnCreate(
            idempotency_key="wis-remix:render-1:variant-1:abcdef12",
            filename="审核通过成片.mp4",
            size=2048,
            sha256="a" * 64,
            category="水润面膜",
            source_asset_ids=[101, 102],
            source_clip_ids=["clip-1", "clip-2"],
            framework_id="framework-1",
            framework_name="成交框架",
            render_id="render-1",
            variant_id="variant-1",
            maker_id="user-1",
            maker_name="测试制作人",
            review_status="approved",
        )
        ticket = {
            "object_key": f"{settings.prefix.rstrip('/')}/uploads/WIS混剪工作台/return.mp4",
            "upload_url": "https://oss/upload",
            "public_url": "https://oss/public",
            "headers": {"Content-Type": "video/mp4"},
            "expires_in": 3600,
        }
        remote = {
            "object_key": ticket["object_key"],
            "filename": "return.mp4",
            "media_type": "video",
            "size": 2048,
            "etag": "etag",
            "modified_at": datetime.utcnow(),
        }
        with SessionLocal() as db, patch.object(
            oss_service, "create_upload", return_value=ticket
        ) as create_upload, patch.object(
            oss_service, "head_asset", side_effect=[None, remote, remote, remote]
        ), patch.object(
            oss_service, "url_for", side_effect=lambda key, **_: f"https://oss/{key}"
        ):
            created = workstation_return_presign(payload, db, {})
            resumed = workstation_return_presign(payload, db, {})
            completed = workstation_return_complete(
                WorkstationReturnComplete(idempotency_key=payload.idempotency_key),
                db,
                {},
            )
            readback = workstation_return_status(payload.idempotency_key, db, {})
            asset_count = db.scalar(select(func.count()).select_from(Asset))
            return_count = db.scalar(select(func.count()).select_from(WorkstationReturn))

        self.assertTrue(created["upload_required"])
        self.assertFalse(resumed["upload_required"])
        self.assertEqual(create_upload.call_count, 1)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(readback["asset_id"], completed["asset_id"])
        self.assertEqual(readback["provenance"]["source_asset_ids"], [101, 102])
        self.assertEqual(asset_count, 1)
        self.assertEqual(return_count, 1)

    def test_workstation_qianchuan_retry_reuses_uploaded_video_and_resumes_binding(
        self,
    ):
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/uploads/remix/auto.mp4",
                filename="自动混剪-黑晶面膜.mp4",
                media_type="video",
                size=2048,
                modified_at=datetime.utcnow(),
                category="黑晶面膜",
                library_type="remix",
                asset_subtype="AI混剪成片",
                source="wis_remix_workstation",
            )
            db.add(asset)
            db.flush()
            task = QianchuanDelivery(
                id="task-workstation-retry",
                batch_id="batch-workstation-retry",
                asset_id=asset.id,
                created_by_number="FD-TEST",
                advertiser_id="1869672250595328",
                plan_id="1870289794646204",
                plan_type="multiplication",
                platform_asset_id="qianchuan-video-id",
                idempotency_key="wis-remix:render:variant:asset:account:plan",
                status="partial",
                failure_stage="plan_binding",
                error_message="计划绑定失败",
            )
            db.add(task)
            db.commit()

            with patch("app.main._require_assets_review_approved"):
                result = workstation_qianchuan_delivery_retry(
                    task.idempotency_key,
                    db,
                    {},
                )
            db.refresh(task)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.platform_asset_id, "qianchuan-video-id")
        self.assertIn("不会重复上传", task.message)
        self.assertEqual(task.error_message, "")


if __name__ == "__main__":
    unittest.main()
