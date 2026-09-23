import asyncio
import json
import hashlib
import os
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import ANY, MagicMock, PropertyMock, patch
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import BackgroundTasks, HTTPException, Request
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.database import Base, SessionLocal, engine
from app.auth import _password_grant_payload, login_with_password, require_user, user_allowed
from app.config import settings
from app.oss_service import display_filename_from_object_key, normalize_upload_filename, oss_service
from app.main import (
    AccessGrantCreate,
    ModuleAccessBatchUpdate,
    BatchDeletePayload,
    BatchFolderPayload,
    BatchTagsPayload,
    TrashClearPayload,
    acquire_upload_lease,
    release_upload_lease,
    _upload_leases,
    create_multipart_upload_session,
    multipart_upload_part_urls,
    complete_multipart_upload_session,
    multipart_upload_part_relay,
    cancel_multipart_upload_session,
    admin_access_grant_create,
    admin_access_grant_revoke,
    admin_access_grants,
    admin_module_access_batch_update,
    central_auth_me,
    module_access_for_user,
    batch_delete_assets,
    batch_update_asset_folder,
    batch_update_asset_tags,
    claim_qianchuan_tasks,
    select_qianchuan_metrics_task_ids,
    run_qianchuan_auto_metrics_if_due,
    run_qianchuan_daily_metrics_if_due,
    _daily_metric_tasks,
    _read_plan_daily_with_retry,
    _run_daily_metric_date,
    _task_daily_summary,
    _asset_platform_gmv_summary,
    _qianchuan_job_slots,
    _qianchuan_metrics_slot,
    _sync_qianchuan_task_metrics,
    _merge_oss_assets,
    repair_upload_asset_filenames,
    _invalidate_catalog_cache,
    infer_product_image_type,
    _CENTRAL_AUTH_EXEMPT_PATHS,
    liveness,
    favorite_asset_ids,
    can_mark_effective_asset,
    can_manage_asset,
    can_delete_asset,
    ensure_asset_schema,
    is_asset_admin,
    is_super_admin,
    list_assets,
    workstation_assets,
    delete_asset,
    list_trash,
    permanently_delete_asset,
    purge_all_trash,
    product_image_categories,
    facets,
    facets_bundle,
    canonical_product_category,
    update_asset,
    purge_deleted_assets,
    run_trash_cleanup_if_due,
    restore_deleted_asset,
    restore_asset,
    qianchuan_push,
    qianchuan_plans,
    qianchuan_plan_materials,
    qianchuan_batch_retry_failed,
    qianchuan_task_delete,
    qianchuan_task_retry,
    qianchuan_metrics_sync,
    qianchuan_tasks,
    workstation_qianchuan_push,
    workstation_qianchuan_accounts,
    workstation_qianchuan_product_plan_map,
    workstation_qianchuan_plans,
    workstation_qianchuan_target_verify,
    workstation_qianchuan_delivery,
    _register_workstation_automatic_review,
    adq_push,
    adq_shared_library_upload,
    adq_tasks,
    adq_batch_retry_failed,
    run_adq_push,
    _apply_adq_metrics_result,
    soft_delete_asset,
    stats,
    set_asset_effective,
    import_effective_asset_to_clip_library,
    effective_asset_clip_library_status,
    upload_analytics,
)
from app.models import (
    AdqDelivery,
    AdqMetricDaily,
    AppMeta,
    Asset,
    AssetAuditLog,
    AssetEffectiveMark,
    AssetFavorite,
    ChannelsAccount,
    ChannelsDelivery,
    ChannelsMetricDaily,
    OaAccessAuditLog,
    OaAccessGrant,
    ModuleAccessGrant,
    QianchuanDelivery,
    QianchuanMetricDaily,
    WorkstationReturn,
    UploadSession,
)
from app.schemas import AdqPushCreate, AdqSharedUploadCreate, AdqTarget, AssetEffectiveUpdate, AssetUpdate, MultipartPartUrlsCreate, MultipartUploadComplete, MultipartUploadedPart, MultipartUploadCreate, QianchuanMetricsSync, QianchuanPushCreate, QianchuanTarget, WorkstationQianchuanPushCreate
from app.catalog_service import infer_category, infer_content_type, infer_library_metadata
from app.adq_service import AdqError, AdqService, adq_service
from app.oss_service import OssService
from app.qianchuan_service import (
    ACCOUNTS_URL,
    ASYNC_VIDEO_UPLOAD_RESULT_URL,
    ASYNC_VIDEO_UPLOAD_URL,
    EBP_ADVERTISERS_URL,
    FULL_DOMAIN_MATERIAL_LIST_URL,
    MATERIAL_REPORT_URL,
    QianchuanError,
    QianchuanService,
    qianchuan_service,
)


class CoreBehaviorTests(unittest.TestCase):
    def test_liveness_probe_does_not_depend_on_database_aggregates(self):
        self.assertEqual(asyncio.run(liveness()), {"status": "ok"})
        self.assertIn("/api/live", _CENTRAL_AUTH_EXEMPT_PATHS)

    def test_feedback_categories_and_content_types_are_canonical(self):
        self.assertEqual(infer_category("燕窝胜肽面膜成片"), "燕窝面膜")
        self.assertEqual(infer_category("清洁泥膜展示"), "其他 WIS 素材")
        self.assertEqual(infer_content_type("上脸敷贴效果"), "上脸展示")
        self.assertEqual(infer_content_type("数字人口播"), "数字人")
        self.assertEqual(infer_content_type("图文轮播"), "图文")

    def test_upload_filename_keeps_chinese_and_repairs_common_mojibake(self):
        expected = "测试文件.mp4"
        mojibake = expected.encode("utf-8").decode("gbk")
        self.assertEqual(expected, normalize_upload_filename(expected))
        self.assertEqual(expected, normalize_upload_filename(mojibake))
        self.assertEqual(expected, normalize_upload_filename("%E6%B5%8B%E8%AF%95%E6%96%87%E4%BB%B6.mp4"))
        self.assertEqual(
            "0817-黑晶素材.mp4",
            display_filename_from_object_key("yxb/uploads/同事/20260817T053555Z-d3a47e667d-0817-黑晶素材.mp4"),
        )
        self.assertEqual("正常-0817素材.mp4", display_filename_from_object_key("正常-0817素材.mp4"))

    def test_upload_part_leases_limit_one_user_and_release_capacity(self):
        _upload_leases.clear()
        leases = [acquire_upload_lease("FD-A", "session-a") for _ in range(settings.upload_user_part_limit)]
        self.assertTrue(all(item["acquired"] for item in leases))
        blocked = acquire_upload_lease("FD-A", "session-a")
        self.assertFalse(blocked["acquired"])
        self.assertTrue(release_upload_lease("FD-A", leases[0]["lease_id"]))
        resumed = acquire_upload_lease("FD-A", "session-a")
        self.assertTrue(resumed["acquired"])
        _upload_leases.clear()

    def test_upload_part_waiters_report_position_and_rotate_between_colleagues(self):
        from app.main import _upload_waiters

        _upload_leases.clear()
        _upload_waiters.clear()
        leases = [acquire_upload_lease(f"FD-{index:02d}", "busy") for index in range(settings.upload_global_part_limit)]
        self.assertTrue(all(item["acquired"] for item in leases))
        first = acquire_upload_lease("FD-A", "session-a", "request-a1")
        second = acquire_upload_lease("FD-A", "session-a", "request-a2")
        colleague = acquire_upload_lease("FD-B", "session-b", "request-b1")
        self.assertEqual((first["queue_position"], second["queue_position"], colleague["queue_position"]), (1, 2, 2))
        self.assertTrue(release_upload_lease("FD-00", leases[0]["lease_id"]))
        acquired = acquire_upload_lease("FD-A", "session-a", "request-a1")
        self.assertTrue(acquired["acquired"])
        # After A receives a lane, the next free lane is reserved for B.
        a_second = acquire_upload_lease("FD-A", "session-a", "request-a2")
        self.assertEqual(a_second["queue_position"], 2)
        _upload_leases.clear()
        _upload_waiters.clear()

    def test_multipart_upload_session_resumes_and_completes_verified_parts(self):
        owner = {"number": "FD-A", "realName": "甲"}
        size = 40 * 1024 * 1024
        payload = MultipartUploadCreate(
            filename="原视频.mp4",
            content_type="video/mp4",
            size=size,
            sha256="a" * 64,
            asset_scope="marketing_video",
            category="通用",
        )
        with SessionLocal() as db, patch.object(
            oss_service, "create_object_key", return_value="yxb/uploads/甲/session-original.mp4",
        ), patch.object(
            oss_service, "create_multipart_upload", return_value="oss-upload-id",
        ), patch.object(
            oss_service, "list_multipart_parts", return_value=[],
        ):
            created = create_multipart_upload_session(payload, db, owner)
            resumed = create_multipart_upload_session(payload, db, owner)
            self.assertEqual(created["session_id"], resumed["session_id"])
            self.assertEqual(created["total_parts"], 2)
            with patch.object(oss_service, "presign_upload_part", side_effect=lambda key, upload_id, number: f"https://oss.example/part/{number}"):
                urls = multipart_upload_part_urls(
                    created["session_id"], MultipartPartUrlsCreate(part_numbers=[1, 2]), db, owner,
                )
            self.assertEqual([item["part_number"] for item in urls["items"]], [1, 2])

            parts = [
                {"part_number": 1, "etag": "etag-1", "size": 32 * 1024 * 1024},
                {"part_number": 2, "etag": "etag-2", "size": 8 * 1024 * 1024},
            ]
            completion = MultipartUploadComplete(
                parts=[MultipartUploadedPart(**item) for item in parts],
                sha256="c" * 64,
            )
            with patch.object(oss_service, "complete_multipart_upload",
            ) as complete_mock, patch.object(
                oss_service,
                "head_asset",
                return_value={"object_key": created["object_key"], "filename": "原视频.mp4", "media_type": "video", "size": size, "etag": "merged", "modified_at": datetime.utcnow()},
            ):
                completed = complete_multipart_upload_session(created["session_id"], completion, db, owner)
            self.assertEqual(completed["status"], "completed")
            complete_mock.assert_called_once()
            stored = db.get(UploadSession, created["session_id"])
            self.assertEqual(stored.status, "completed")
            self.assertEqual(stored.sha256, "c" * 64)

    def test_multipart_completion_accepts_cached_client_aliases_and_missing_sizes(self):
        owner = {"number": "FD-A", "realName": "甲"}
        size = 40 * 1024 * 1024
        payload = MultipartUploadCreate(
            filename="缓存页面原视频.mp4",
            content_type="video/mp4",
            size=size,
            sha256="a" * 64,
            asset_scope="marketing_video",
            category="混剪成片",
        )
        with SessionLocal() as db, patch.object(
            oss_service, "create_multipart_upload", return_value="upload-cached"
        ):
            created = create_multipart_upload_session(payload, db, owner)
            legacy_payload = {
                "uploadedParts": [
                    {"partNumber": 1, "ETag": '"etag-1"'},
                    {"partNumber": 2, "ETag": '"etag-2"'},
                ],
                "hash": "b" * 64,
            }
            with patch.object(oss_service, "complete_multipart_upload") as complete_mock, patch.object(
                oss_service,
                "head_asset",
                return_value={"object_key": created["object_key"], "filename": "缓存页面原视频.mp4", "media_type": "video", "size": size, "etag": "merged", "modified_at": datetime.utcnow()},
            ):
                completed = complete_multipart_upload_session(created["session_id"], legacy_payload, db, owner)
            self.assertEqual(completed["status"], "completed")
            sent_parts = complete_mock.call_args.args[2]
            self.assertEqual([item["size"] for item in sent_parts], [32 * 1024 * 1024, 8 * 1024 * 1024])
            self.assertEqual(db.get(UploadSession, created["session_id"]).sha256, "b" * 64)

    def test_multipart_resume_survives_list_parts_access_denied(self):
        owner = {"number": "FD-A", "realName": "上传同事"}
        payload = MultipartUploadCreate(
            filename="原视频.mp4",
            content_type="video/mp4",
            size=40 * 1024 * 1024,
            sha256="b" * 64,
            asset_scope="marketing_video",
            category="通用",
        )
        with SessionLocal() as db, patch.object(
            oss_service, "create_object_key", return_value="yxb/uploads/user/session-original.mp4",
        ), patch.object(
            oss_service, "create_multipart_upload", return_value="oss-upload-id",
        ):
            created = create_multipart_upload_session(payload, db, owner)
            with patch.object(oss_service, "list_multipart_parts", side_effect=PermissionError("AccessDenied")):
                resumed = create_multipart_upload_session(payload, db, owner)
            self.assertEqual(created["session_id"], resumed["session_id"])
            self.assertEqual(resumed["status"], "active")
            self.assertEqual(resumed["uploaded_parts"], [])
            self.assertEqual(db.get(UploadSession, created["session_id"]).status, "active")

    def test_multipart_cancel_aborts_oss_and_marks_session_cancelled(self):
        owner = {"number": "FD-CANCEL", "realName": "取消测试"}
        payload = MultipartUploadCreate(
            filename="可取消原视频.mp4",
            content_type="video/mp4",
            size=40 * 1024 * 1024,
            sha256="d" * 64,
            asset_scope="marketing_video",
            category="通用",
        )
        with SessionLocal() as db, patch.object(
            oss_service, "create_multipart_upload", return_value="oss-cancel-id",
        ):
            created = create_multipart_upload_session(payload, db, owner)
            with patch.object(oss_service, "abort_multipart_upload") as abort:
                result = cancel_multipart_upload_session(created["session_id"], db, owner)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(db.get(UploadSession, created["session_id"]).status, "cancelled")
            abort.assert_called_once_with(created["object_key"], "oss-cancel-id")

    def test_multipart_relay_upload_verifies_bytes_and_returns_receipt(self):
        owner = {"number": "FD-RELAY", "realName": "中转测试"}
        content = b"durable-upload"
        payload = MultipartUploadCreate(
            filename="中转原视频.mp4",
            content_type="video/mp4",
            size=len(content),
            sha256="e" * 64,
            asset_scope="marketing_video",
            category="通用",
        )

        class RelayRequest:
            headers = {
                "content-length": str(len(content)),
                "x-upload-part-md5": hashlib.md5(content, usedforsecurity=False).hexdigest(),
            }

            async def stream(self):
                yield content[:5]
                yield content[5:]

        captured = {}

        def relay_part(key, upload_id, part_number, body, content_length, content_md5):
            captured.update(
                key=key,
                upload_id=upload_id,
                part_number=part_number,
                body=body.read(),
                content_length=content_length,
                content_md5=content_md5,
            )
            return '"relay-etag"'

        with SessionLocal() as db, patch.object(
            oss_service, "create_multipart_upload", return_value="oss-relay-id",
        ), patch.object(
            oss_service, "upload_multipart_part", side_effect=relay_part,
        ):
            created = create_multipart_upload_session(payload, db, owner)
            result = asyncio.run(
                multipart_upload_part_relay(
                    created["session_id"], 1, RelayRequest(), db, owner,
                )
            )

        self.assertEqual(result, {
            "part_number": 1,
            "etag": '"relay-etag"',
            "size": len(content),
            "relayed": True,
        })
        self.assertEqual(captured["body"], content)
        self.assertEqual(captured["content_length"], len(content))
        self.assertEqual(captured["part_number"], 1)

    def test_oss_multipart_relay_uses_presigned_put_with_md5(self):
        content = b"presigned-relay"
        response = SimpleNamespace(status_code=200, headers={"ETag": '"relay-etag"'})
        with patch.object(oss_service, "client", object()), patch.object(
            oss_service, "presign_upload_part", return_value="https://oss.example.test/signed",
        ) as presign, patch("app.oss_service.requests.put", return_value=response) as put:
            etag = oss_service.upload_multipart_part(
                "uploads/test.mp4",
                "upload-id",
                2,
                BytesIO(content),
                len(content),
                "base64-md5",
            )

        self.assertEqual(etag, '"relay-etag"')
        presign.assert_called_once_with("uploads/test.mp4", "upload-id", 2)
        self.assertEqual(put.call_args.kwargs["headers"], {
            "Content-Length": str(len(content)),
            "Content-MD5": "base64-md5",
        })
        self.assertEqual(put.call_args.kwargs["timeout"], (15, 300))

    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        ensure_asset_schema()

    def setUp(self):
        with SessionLocal() as db:
            db.query(UploadSession).delete()
            db.query(OaAccessAuditLog).delete()
            db.query(ModuleAccessGrant).delete()
            db.query(OaAccessGrant).delete()
            db.query(QianchuanMetricDaily).delete()
            db.query(QianchuanDelivery).delete()
            db.query(AdqMetricDaily).delete()
            db.query(AdqDelivery).delete()
            db.query(ChannelsMetricDaily).delete()
            db.query(ChannelsDelivery).delete()
            db.query(ChannelsAccount).delete()
            db.query(AppMeta).delete()
            db.query(AssetAuditLog).delete()
            db.query(AssetEffectiveMark).delete()
            db.query(AssetFavorite).delete()
            db.query(Asset).delete()
            db.commit()
        _invalidate_catalog_cache()

    def test_upload_analytics_separates_libraries_people_and_verified_gmv(self):
        with SessionLocal() as db:
            source_asset = Asset(
                object_key="yxb/uploads/fd-a/source.mp4",
                filename="source.mp4",
                media_type="video",
                asset_scope="marketing_video",
                ingest_source="oa_upload",
                library_type="source",
                uploaded_by_number="FD-A",
                uploaded_by_name="同事甲",
                modified_at=datetime(2026, 8, 12, 1, 0),
            )
            remix_asset = Asset(
                object_key="yxb/uploads/fd-b/remix.mp4",
                filename="remix.mp4",
                media_type="video",
                asset_scope="marketing_video",
                ingest_source="oa_upload",
                library_type="remix",
                uploaded_by_number="FD-B",
                uploaded_by_name="同事乙",
                modified_at=datetime(2026, 8, 12, 2, 0),
            )
            scanned_asset = Asset(
                object_key="yxb/scanned.mp4",
                filename="scanned.mp4",
                media_type="video",
                asset_scope="marketing_video",
                ingest_source="oss_scan",
                library_type="remix",
                modified_at=datetime(2026, 8, 12, 3, 0),
            )
            db.add_all([source_asset, remix_asset, scanned_asset])
            db.flush()
            tasks = [
                QianchuanDelivery(
                    id="analytics-source-old",
                    batch_id="analytics",
                    asset_id=source_asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2001",
                    platform_asset_id="video-source",
                    status="success",
                ),
                QianchuanDelivery(
                    id="analytics-source-new",
                    batch_id="analytics",
                    asset_id=source_asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2001",
                    platform_asset_id="video-source",
                    status="success",
                ),
                QianchuanDelivery(
                    id="analytics-remix",
                    batch_id="analytics",
                    asset_id=remix_asset.id,
                    created_by_number="FD-B",
                    advertiser_id="1002",
                    plan_id="2002",
                    platform_asset_id="video-remix",
                    status="success",
                ),
            ]
            db.add_all(tasks)
            db.flush()
            db.add_all([
                QianchuanMetricDaily(
                    task_id="analytics-source-old",
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="video-source",
                    stat_date="2026-08-12",
                    status="success",
                    has_data=True,
                    link_verified=True,
                    metrics={"pay_order_amount": 5000},
                ),
                QianchuanMetricDaily(
                    task_id="analytics-source-new",
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="video-source",
                    stat_date="2026-08-12",
                    status="success",
                    has_data=True,
                    link_verified=True,
                    metrics={"pay_order_amount": 60000},
                ),
                QianchuanMetricDaily(
                    task_id="analytics-remix",
                    advertiser_id="1002",
                    plan_id="2002",
                    video_id="video-remix",
                    stat_date="2026-08-12",
                    status="success",
                    has_data=True,
                    link_verified=True,
                    metrics={"pay_order_amount": 2000},
                ),
            ])
            db.commit()

            result = upload_analytics(date(2026, 8, 12), date(2026, 8, 12), db, {"number": "FD-ADMIN"})

        self.assertEqual(result["summary"], {"total": 2, "source": 1, "remix": 1, "transacted": 2, "hits": 1})
        self.assertEqual(result["daily"][0]["total"], 2)
        self.assertEqual(result["daily"][0]["transacted"], 2)
        self.assertEqual(result["daily"][0]["hits"], 1)
        people = {item["number"]: item for item in result["contributors"]}
        self.assertEqual(people["FD-A"]["source"], 1)
        self.assertEqual(people["FD-A"]["hits"], 1)
        self.assertEqual(people["FD-B"]["remix"], 1)
        self.assertEqual(result["coverage"]["verified_rows"], 2)

    def test_upload_analytics_marks_unavailable_gmv_as_pending_not_zero(self):
        with SessionLocal() as db:
            db.add(Asset(
                object_key="yxb/uploads/no-metrics.mp4",
                filename="no-metrics.mp4",
                media_type="video",
                asset_scope="marketing_video",
                ingest_source="oa_upload",
                library_type="remix",
                uploaded_by_number="FD-A",
                uploaded_by_name="同事甲",
                modified_at=datetime(2026, 8, 12, 1, 0),
            ))
            db.commit()
            result = upload_analytics(date(2026, 8, 12), date(2026, 8, 12), db, {"number": "FD-ADMIN"})

        self.assertEqual(result["summary"]["total"], 1)
        self.assertIsNone(result["summary"]["transacted"])
        self.assertIsNone(result["summary"]["hits"])
        self.assertEqual(result["coverage"]["state"], "unavailable")

    def test_upload_analytics_excludes_jianying_exports_from_upload_volume(self):
        with SessionLocal() as db:
            db.add_all([
                Asset(
                    object_key="yxb/uploads/manual.mp4",
                    filename="manual.mp4",
                    media_type="video",
                    asset_scope="marketing_video",
                    ingest_source="oa_upload",
                    source="oa_upload",
                    library_type="source",
                    uploaded_by_number="FD-MANUAL",
                    uploaded_by_name="人工上传同事",
                    modified_at=datetime(2026, 8, 12, 1, 0),
                ),
                Asset(
                    object_key="yxb/uploads/jianying.mp4",
                    filename="jianying.mp4",
                    media_type="video",
                    asset_scope="marketing_video",
                    ingest_source="oa_upload",
                    source="jianying_export",
                    library_type="remix",
                    uploaded_by_number="FD-JIANYING",
                    uploaded_by_name="剪映互传同事",
                    modified_at=datetime(2026, 8, 12, 2, 0),
                ),
            ])
            db.commit()
            result = upload_analytics(date(2026, 8, 12), date(2026, 8, 12), db, {"number": "FD-ADMIN"})

        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["summary"]["source"], 1)
        self.assertEqual(result["summary"]["remix"], 0)
        people = {item["number"]: item for item in result["contributors"]}
        self.assertIn("FD-MANUAL", people)
        self.assertNotIn("FD-JIANYING", people)

    def test_oa_login_follows_password_grant_then_userinfo_contract(self):
        responses = [
            (200, {"data": {"accessToken": "oa-token"}}, {}),
            (
                200,
                {
                    "code": 0,
                    "data": {
                        "number": "FD-0001",
                        "realName": "测试用户",
                        "groupName": "其他部门",
                        "status": "normal",
                    },
                },
                {},
            ),
        ]
        with patch.dict(
            os.environ,
            {"OA_ENFORCE_DEPARTMENT": "false", "OA_CLIENT_ID": "", "OA_CLIENT_SECRET": ""},
            clear=False,
        ):
            with patch("app.auth._request_json", side_effect=responses) as request_json:
                token, user = login_with_password(" fd-0001 ", "secret")

        self.assertEqual(token, "oa-token")
        self.assertEqual(user["number"], "FD-0001")
        first_call, second_call = request_json.call_args_list
        self.assertEqual(first_call.args[0], "https://api.fandow.com/authentication/password-grant")
        self.assertEqual(first_call.kwargs["method"], "POST")
        self.assertEqual(first_call.kwargs["payload"], {"username": "FD-0001", "password": "secret"})
        self.assertEqual(second_call.args[0], "https://api.fandow.com/auth/userinfo")
        self.assertEqual(second_call.kwargs["token"], "oa-token")

    def test_oa_login_includes_current_client_fields_and_captcha_when_configured(self):
        environment = {
            "OA_CLIENT_ID": "oa-client",
            "OA_CLIENT_SECRET": "server-side-secret",
            "OA_GRANT_TYPE": "password",
        }
        with patch.dict(os.environ, environment, clear=False):
            payload = _password_grant_payload("FD-0001", "secret", "123456")

        self.assertEqual(payload["username"], "FD-0001")
        self.assertEqual(payload["password"], "secret")
        self.assertEqual(payload["captcha"], "123456")
        self.assertEqual(payload["client_id"], "oa-client")
        self.assertEqual(payload["client_secret"], "server-side-secret")
        self.assertEqual(payload["grant_type"], "password")

    def test_oa_code_400_requests_captcha_instead_of_reporting_password_error(self):
        response = (422, {"code": 400, "msg": "请完成验证码校验"}, {})
        with patch("app.auth._request_json", return_value=response):
            with self.assertRaises(HTTPException) as blocked:
                login_with_password("fd-0001", "secret")
        self.assertEqual(blocked.exception.status_code, 428)

    def test_oa_request_falls_back_from_stale_app_cookie_to_verified_gateway_token(self):
        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"cookie", b"wis_oa_session=stale-token"),
                    (b"x-oa-token", b"gateway-token"),
                ],
            }
        )
        valid_user = {
            "number": "FD-022896",
            "realName": "测试同事",
            "groupName": "品牌营销部",
            "status": "normal",
        }
        with patch.dict(os.environ, {"OA_ENFORCE_DEPARTMENT": "false"}, clear=False), patch(
            "app.auth.user_from_token",
            side_effect=[HTTPException(401, "expired"), valid_user],
        ):
            user = require_user(request)
        self.assertEqual(user["number"], "FD-022896")

    def test_oa_login_surfaces_safe_upstream_error_without_logging_password(self):
        response = (401, {"code": 40101, "msg": "账号已锁定，请完成验证码校验"}, {})
        with patch("app.auth._request_json", return_value=response), patch("app.auth.logger.warning") as warning:
            with self.assertRaises(HTTPException) as blocked:
                login_with_password("fd-0001", "super-secret-password")

        self.assertEqual(blocked.exception.status_code, 401)
        self.assertIn("账号已锁定，请完成验证码校验", blocked.exception.detail)
        self.assertIn("40101", blocked.exception.detail)
        self.assertNotIn("super-secret-password", blocked.exception.detail)
        self.assertNotIn("super-secret-password", str(warning.call_args))

    def test_oa_access_requires_normal_status_and_department_filter_is_optional(self):
        normal_other_department = {"status": "normal", "groupName": "产品中心"}
        frozen_brand_user = {"status": "freeze", "groupName": "品牌营销部"}

        with patch.dict(os.environ, {"OA_ENFORCE_DEPARTMENT": "false"}, clear=False):
            self.assertTrue(user_allowed(normal_other_department))
            self.assertFalse(user_allowed(frozen_brand_user))
        with patch.dict(
            os.environ,
            {"OA_ENFORCE_DEPARTMENT": "true", "OA_ALLOWED_DEPARTMENT": "品牌营销"},
            clear=False,
        ):
            self.assertFalse(user_allowed(normal_other_department))
            self.assertTrue(user_allowed({"status": "normal", "groupName": "品牌营销部"}))

    def test_oa_access_supports_multiple_automatic_department_rules(self):
        environment = {
            "OA_ENFORCE_DEPARTMENT": "true",
            "OA_ALLOWED_DEPARTMENTS": "品牌营销,品牌管理部-WIS品牌中心",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertTrue(user_allowed({"status": "normal", "groupName": "品牌营销部"}))
            self.assertTrue(user_allowed({"status": "normal", "groupName": "品牌管理部-WIS品牌中心"}))
            self.assertFalse(user_allowed({"status": "normal", "groupName": "品牌管理部-其他中心"}))
            self.assertFalse(user_allowed({"status": "freeze", "groupName": "品牌管理部-WIS品牌中心"}))

    def test_brand_department_auto_access_overrides_stale_inactive_exception(self):
        with SessionLocal() as db:
            db.add(OaAccessGrant(
                identifier="name:品牌营销部同事",
                identifier_type="name",
                real_name="品牌营销部同事",
                active=False,
                source="admin",
            ))
            db.commit()
        environment = {
            "OA_ENFORCE_DEPARTMENT": "true",
            "OA_ALLOWED_DEPARTMENTS": "品牌营销,品牌管理部-WIS品牌中心",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertTrue(user_allowed({
                "status": "normal",
                "realName": "品牌营销部同事",
                "groupName": "品牌营销部-直播中心",
            }))

    def test_central_auth_me_returns_normalized_real_identity_fields(self):
        user = {
            "number": "FD-027340",
            "realName": "刘慧迅",
            "parentDept": "品牌营销部",
            "groupName": "品牌营销部-直播中心",
            "positionName": "直播高级经理",
            "avatar": {"avatar_240": "https://example.test/avatar.png"},
            "status": "normal",
        }
        result = central_auth_me(user)
        self.assertEqual(result["user"]["department"], "品牌营销部")
        self.assertEqual(result["user"]["center"], "品牌营销部-直播中心")
        self.assertEqual(result["user"]["jobTitle"], "直播高级经理")
        self.assertEqual(result["user"]["avatarUrl"], "https://example.test/avatar.png")
        self.assertEqual(len(result["access"]["allowed_modules"]), 8)
        self.assertIn("creative-radar", result["access"]["allowed_modules"])
        self.assertIn("live-room-management", result["access"]["allowed_modules"])

    def test_oa_access_allows_only_named_personal_exceptions(self):
        environment = {
            "OA_ENFORCE_DEPARTMENT": "true",
            "OA_ALLOWED_DEPARTMENT": "品牌营销",
            "OA_ALLOWED_USERS": "叶森莹,朱嘉琳",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertTrue(user_allowed({"status": "normal", "realName": "叶森莹", "groupName": "AI效率流程部"}))
            self.assertTrue(user_allowed({"status": "normal", "name": "朱嘉琳", "groupName": "AI效率流程部"}))
            self.assertFalse(user_allowed({"status": "normal", "realName": "其他同事", "groupName": "AI效率流程部"}))
            self.assertFalse(user_allowed({"status": "freeze", "realName": "叶森莹", "groupName": "AI效率流程部"}))

    def test_super_admin_can_manage_persistent_oa_access_grants(self):
        admin = {"number": "FD-026222", "realName": "舒豪", "groupName": "品牌营销部"}
        colleague = {"number": "FD-099999", "realName": "外部协作同事", "groupName": "AI效率流程部", "status": "normal"}
        environment = {
            "SUPER_ADMIN_NUMBERS": "FD-026222",
            "OA_ALLOWED_USERS": "外部协作同事",
            "OA_ENFORCE_DEPARTMENT": "true",
            "OA_ALLOWED_DEPARTMENT": "品牌营销",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertTrue(is_super_admin(admin))
            self.assertFalse(is_super_admin(colleague))
            with SessionLocal() as db:
                created = admin_access_grant_create(
                    AccessGrantCreate(real_name="外部协作同事", department="AI效率流程部"),
                    db,
                    admin,
                )
                self.assertTrue(created["active"])
                listed = admin_access_grants("外部协作", True, db, admin)
                self.assertEqual(listed["total"], 1)
                self.assertEqual(len(listed["audits"]), 1)
            self.assertTrue(user_allowed(colleague))
            with SessionLocal() as db:
                revoked = admin_access_grant_revoke(created["identifier"], db, admin)
                self.assertFalse(revoked["active"])
                self.assertEqual(db.query(OaAccessAuditLog).count(), 2)
            self.assertFalse(user_allowed(colleague))

    def test_non_super_admin_cannot_manage_oa_access_grants(self):
        regular = {"number": "FD-012345", "realName": "普通同事", "groupName": "品牌营销部"}
        with patch.dict(os.environ, {"SUPER_ADMIN_NUMBERS": "FD-026222"}, clear=False):
            with SessionLocal() as db, self.assertRaises(HTTPException) as blocked:
                admin_access_grant_create(AccessGrantCreate(real_name="目标同事"), db, regular)
        self.assertEqual(blocked.exception.status_code, 403)

    def test_oa_login_backfills_department_and_center_separately(self):
        with SessionLocal() as db:
            db.add(OaAccessGrant(
                identifier="name:组织信息同事",
                identifier_type="name",
                real_name="组织信息同事",
                department="营销中心C",
                active=True,
            ))
            db.commit()
        user = {
            "number": "FD-ORG-001",
            "realName": "组织信息同事",
            "parentDept": "品牌管理部",
            "groupName": "营销中心C",
            "status": "normal",
        }
        self.assertTrue(user_allowed(user))
        with SessionLocal() as db:
            saved = db.get(OaAccessGrant, "name:组织信息同事")
            self.assertEqual(saved.department, "品牌管理部")
            self.assertEqual(saved.center, "营销中心C")
            self.assertEqual(saved.user_number, "FD-ORG-001")

    def test_super_admin_can_batch_update_module_access_atomically(self):
        admin = {"number": "FD-026222", "realName": "权限负责人"}
        identifiers = ["name:批量同事甲", "name:批量同事乙"]
        with patch.dict(os.environ, {"SUPER_ADMIN_NUMBERS": "FD-026222"}, clear=False), SessionLocal() as db:
            db.add_all([
                OaAccessGrant(identifier=identifiers[0], real_name="批量同事甲", active=True),
                OaAccessGrant(identifier=identifiers[1], real_name="批量同事乙", active=True),
            ])
            db.commit()
            result = admin_module_access_batch_update(
                ModuleAccessBatchUpdate(
                    identifiers=identifiers,
                    access_mode="selected",
                    modules=["creative-hub", "material-workbench"],
                ),
                db,
                admin,
            )
            saved = db.scalars(select(ModuleAccessGrant)).all()
            audits = db.scalars(select(OaAccessAuditLog).where(OaAccessAuditLog.action == "module_scope")).all()
        self.assertEqual(result["updated"], 2)
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(row.access_mode == "selected" for row in saved))
        self.assertTrue(all(row.modules == ["creative-hub", "material-workbench"] for row in saved))
        self.assertEqual(len(audits), 2)

    def test_brand_marketing_member_keeps_creative_radar_with_selected_access(self):
        user = {
            "number": "FD-RADAR-001",
            "realName": "品牌营销专员",
            "groupName": "品牌营销部-营销中心B",
            "status": "normal",
        }
        with SessionLocal() as db:
            db.add(ModuleAccessGrant(
                identifier="number:FD-RADAR-001",
                access_mode="selected",
                modules=["material-workbench"],
            ))
            db.commit()
            access = module_access_for_user(user, db)
        self.assertEqual(access["access_mode"], "selected")
        self.assertEqual(access["allowed_modules"], ["material-workbench", "creative-radar"])

    def test_personal_exception_outside_brand_marketing_does_not_gain_creative_radar(self):
        user = {
            "number": "FD-RADAR-002",
            "realName": "外部例外同事",
            "groupName": "AI效率流程部",
            "status": "normal",
        }
        with SessionLocal() as db:
            db.add(ModuleAccessGrant(
                identifier="number:FD-RADAR-002",
                access_mode="selected",
                modules=["material-workbench"],
            ))
            db.commit()
            access = module_access_for_user(user, db)
        self.assertEqual(access["allowed_modules"], ["material-workbench"])

    def test_batch_module_access_rejects_missing_member_without_partial_write(self):
        admin = {"number": "FD-026222", "realName": "权限负责人"}
        existing = "name:已有成员"
        with patch.dict(os.environ, {"SUPER_ADMIN_NUMBERS": "FD-026222"}, clear=False), SessionLocal() as db:
            db.add(OaAccessGrant(identifier=existing, real_name="已有成员", active=True))
            db.commit()
            with self.assertRaises(HTTPException) as blocked:
                admin_module_access_batch_update(
                    ModuleAccessBatchUpdate(
                        identifiers=[existing, "name:不存在成员"],
                        access_mode="selected",
                        modules=["data-dashboard"],
                    ),
                    db,
                    admin,
                )
            self.assertEqual(blocked.exception.status_code, 404)
            self.assertEqual(db.query(ModuleAccessGrant).count(), 0)

    def test_ingest_source_distinguishes_upload_from_scan(self):
        now = datetime.utcnow()
        rows = [
            {"object_key": "yxb/uploads/张三/a.mp4", "filename": "a.mp4", "media_type": "video", "size": 12, "etag": "a", "modified_at": now},
            {"object_key": "yxb/library/b.mp4", "filename": "b.mp4", "media_type": "video", "size": 13, "etag": "b", "modified_at": now},
        ]
        with patch("app.main.catalog_service.list_assets", return_value=[]):
            with SessionLocal() as db:
                _merge_oss_assets(db, rows, now)
                saved = {item.object_key: item for item in db.query(Asset).all()}
                self.assertEqual(saved["yxb/uploads/张三/a.mp4"].ingest_source, "oa_upload")
                self.assertEqual(saved["yxb/library/b.mp4"].ingest_source, "oss_scan")
                self.assertEqual(saved["yxb/uploads/张三/a.mp4"].library_type, "source")
                self.assertEqual(saved["yxb/library/b.mp4"].library_type, "remix")
                self.assertEqual(saved["yxb/library/b.mp4"].asset_subtype, "其他混剪成片")

    def test_oss_rescan_preserves_upload_display_name_and_repairs_historical_prefix(self):
        now = datetime.utcnow()
        key = "yxb/uploads/同事/20260817T053555Z-d3a47e667d-原始中文名.mp4"
        row = {
            "object_key": key,
            "filename": "20260817T053555Z-d3a47e667d-原始中文名.mp4",
            "media_type": "video",
            "size": 12,
            "etag": "etag",
            "modified_at": now,
        }
        with patch("app.main.catalog_service.list_assets", return_value=[]), SessionLocal() as db:
            asset = Asset(
                object_key=key,
                filename="同事已修改名称.mp4",
                media_type="video",
                size=12,
                ingest_source="oa_upload",
            )
            db.add(asset)
            db.commit()
            _merge_oss_assets(db, [row], now)
            self.assertEqual(db.get(Asset, asset.id).filename, "同事已修改名称.mp4")

            asset.filename = Path(key).name
            db.commit()
            self.assertEqual(repair_upload_asset_filenames(db), 1)
            db.commit()
            self.assertEqual(db.get(Asset, asset.id).filename, "原始中文名.mp4")

    def test_product_image_scope_is_separate_and_keeps_product_filters(self):
        self.assertEqual(infer_product_image_type("WIS面膜10片（不带投影）.png"), "无投影/透明底")
        self.assertEqual(infer_product_image_type("眼膜替换装.png"), "替换装")
        now = datetime.utcnow()
        rows = [
            {
                "object_key": "yxb/product-images/晶润眼膜/甲/WIS新版晶润紧致眼膜3.0俯视.png",
                "filename": "WIS新版晶润紧致眼膜3.0俯视.png",
                "media_type": "image",
                "size": 22,
                "etag": "product",
                "modified_at": now,
            },
            {
                "object_key": "yxb/library/remix.mp4",
                "filename": "remix.mp4",
                "media_type": "video",
                "size": 33,
                "etag": "video",
                "modified_at": now,
            },
        ]
        user = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with patch("app.main.catalog_service.list_assets", return_value=[]):
            with SessionLocal() as db:
                _merge_oss_assets(db, rows, now)
                product = db.query(Asset).filter_by(asset_scope="product_image").one()
                self.assertEqual(product.category, "晶润眼膜")
                self.assertEqual(product.content_type, "俯视")
                self.assertEqual(product.asset_subtype, "产品图片")
                page = list_assets(
                    q="", category="", content_type="", status="", media_type="image",
                    asset_scope="product_image", library_type="", asset_subtype="", ingest_source="",
                    directory="", favorite=None, sort="newest", page=1, page_size=24, db=db, user=user,
                )
                self.assertEqual(page.total, 1)
                self.assertEqual(page.items[0].asset_scope, "product_image")
                categories = product_image_categories(db, user)
                eye_mask = next(item for item in categories["items"] if item["name"] == "晶润眼膜")
                self.assertEqual(eye_mask["count"], 1)
                self.assertEqual(next(item for item in categories["items"] if item["name"] == "通用")["count"], 0)
                self.assertEqual(next(item for item in categories["items"] if item["name"] == "颈膜")["count"], 0)
                self.assertEqual(next(item for item in categories["items"] if item["name"] == "黄金面膜")["count"], 0)
                self.assertEqual(next(item for item in categories["items"] if item["name"] == "美白针")["count"], 0)
                self.assertEqual(next(item for item in categories["items"] if item["name"] == "燕窝面膜")["count"], 0)

    def test_cleanser_categories_are_selectable_before_first_upload(self):
        user = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            bundle = facets_bundle(db=db, _user=user)
            for scope in ("all", "source", "remix"):
                entries = bundle[scope]["categories"]
                for name in ("凝颜洁面", "极润洁面"):
                    self.assertEqual([x for x in entries if x["name"] == name], [{"name": name, "count": 0}])
            products = product_image_categories(db, user)
            self.assertEqual(products["total"], 0)
            for name in ("凝颜洁面", "极润洁面"):
                self.assertEqual([x["count"] for x in products["items"] if x["name"] == name], [0])

    def test_cleanser_category_filters_count_without_reclassifying_history(self):
        with SessionLocal() as db:
            for index, name in enumerate(("凝颜洁面", "凝颜洁面", "极润洁面", "肌活蛋白喷雾", "凝颜")):
                db.add(Asset(object_key=f"yxb/category-test/{index}.mp4", filename=f"{index}.mp4",
                             media_type="video", asset_scope="marketing_video", library_type="source", category=name))
            db.commit()
            before = [(a.id, a.category) for a in db.query(Asset).order_by(Asset.id)]
            result = facets(library_type="source", asset_scope="marketing_video", db=db, _user={})
            counts = {x["name"]: x["count"] for x in result["categories"]}
            self.assertEqual(counts, {"凝颜洁面": 2, "极润洁面": 1, "肌活蛋白喷雾": 1, "凝颜": 1})
            self.assertEqual(len(result["categories"]), 4)
            self.assertEqual(before, [(a.id, a.category) for a in db.query(Asset).order_by(Asset.id)])
            self.assertEqual(canonical_product_category("凝颜"), "凝颜")

    def test_owner_can_save_each_new_cleanser_category(self):
        owner = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/uploads/甲/cleanser.mp4", filename="cleanser.mp4",
                          media_type="video", uploaded_by_number="FD-A", ingest_source="oa_upload")
            db.add(asset)
            db.commit()
            for name in ("凝颜洁面", "极润洁面"):
                result = update_asset(asset.id, AssetUpdate(category=name), db, owner)
                self.assertEqual(result.category, name)
                db.expire_all()
                self.assertEqual(db.get(Asset, asset.id).category, name)

    def test_owner_can_edit_classification_tags_and_reference_fields_after_upload(self):
        owner = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/uploads/甲/editable.mp4",
                filename="editable.mp4",
                media_type="video",
                ingest_source="oa_upload",
                uploaded_by_number="FD-A",
                asset_subtype="其他视频素材",
            )
            db.add(asset)
            db.commit()
            with patch("app.main.oss_service.head_asset", return_value={"media_type": "video", "size": 123}):
                result = update_asset(
                    asset.id,
                    AssetUpdate(
                        category="黄金面膜",
                        content_type="上脸展示",
                        library_type="source",
                        asset_subtype="AI原创素材",
                        tags=["重点标签", "上传素材", "重点标签"],
                        reference_url="https://example.test/competitor",
                        reference_video_key="yxb/references/甲/reference.mp4",
                        reference_video_name="reference.mp4",
                    ),
                    db,
                    owner,
                )
            self.assertEqual(result.category, "黄金面膜")
            self.assertEqual(result.content_type, "上脸展示")
            self.assertEqual(result.asset_subtype, "AI原创素材")
            self.assertEqual(result.tags, ["重点标签", "上传素材"])
            self.assertEqual(result.reference_url, "https://example.test/competitor")
            self.assertEqual(result.reference_video_name, "reference.mp4")

    def test_owner_can_save_ai_material_description_and_performance_screenshots(self):
        owner = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/uploads/甲/ai-original.mp4",
                filename="ai-original.mp4",
                media_type="video",
                ingest_source="oa_upload",
                uploaded_by_number="FD-A",
                asset_subtype="AI原创素材",
            )
            db.add(asset)
            db.commit()
            screenshot = {
                "object_key": "yxb/references/甲/performance-1.png",
                "filename": "performance-1.png",
            }
            with patch("app.main.oss_service.head_asset", return_value={"media_type": "image", "size": 2048}):
                result = update_asset(
                    asset.id,
                    AssetUpdate(
                        material_description="前三秒直接展示产品，转化表现较好。",
                        performance_screenshots=[screenshot],
                    ),
                    db,
                    owner,
                )
            self.assertEqual(result.material_description, "前三秒直接展示产品，转化表现较好。")
            self.assertEqual(result.performance_screenshots[0]["filename"], "performance-1.png")
            self.assertTrue(result.performance_screenshots[0]["url"])

    def test_business_cutover_moves_all_existing_assets_to_remix_once(self):
        with SessionLocal() as db:
            db.add_all([
                Asset(object_key="yxb/legacy/source.mp4", filename="普通历史素材.mp4", media_type="video", library_type="source", asset_subtype="其他视频素材"),
                Asset(object_key="yxb/legacy/ai.mp4", filename="AI历史素材.mp4", media_type="video", library_type="source", asset_subtype="AI原创素材"),
            ])
            db.commit()
        ensure_asset_schema()
        with SessionLocal() as db:
            saved = {item.filename: item for item in db.query(Asset).all()}
            self.assertEqual(saved["普通历史素材.mp4"].library_type, "remix")
            self.assertEqual(saved["普通历史素材.mp4"].asset_subtype, "其他混剪成片")
            self.assertEqual(saved["AI历史素材.mp4"].asset_subtype, "AI混剪成片")
            marker = db.get(AppMeta, "asset_library_business_cutover_20260811_v2")
            self.assertIsNotNone(marker)

        with SessionLocal() as db:
            saved["普通历史素材.mp4"] = db.query(Asset).filter_by(filename="普通历史素材.mp4").one()
            saved["普通历史素材.mp4"].library_type = "source"
            saved["普通历史素材.mp4"].asset_subtype = "实拍自产素材"
            db.commit()
        ensure_asset_schema()
        with SessionLocal() as db:
            preserved = db.query(Asset).filter_by(filename="普通历史素材.mp4").one()
            self.assertEqual(preserved.library_type, "source")
            self.assertEqual(preserved.asset_subtype, "实拍自产素材")

    def test_library_metadata_separates_source_footage_and_remix_outputs(self):
        self.assertEqual(infer_library_metadata("明星信息流原片.mp4"), ("source", "明星信息流原片"))
        self.assertEqual(infer_library_metadata("AI 黑晶混剪成片.mp4"), ("remix", "AI混剪成片"))
        self.assertEqual(infer_library_metadata("门店实拍自产.mov"), ("source", "实拍自产素材"))
        self.assertEqual(infer_library_metadata("水润面膜产品镜-精华质地.mp4"), ("source", "产品镜"))
        self.assertEqual(infer_library_metadata("真实用户KOC原片.mp4"), ("source", "达人/KOC原片"))
        self.assertEqual(infer_library_metadata("WIS品牌创意广告.mp4"), ("source", "品牌创意广告"))
        self.assertEqual(infer_library_metadata("年度品牌IP广告.mp4"), ("source", "品牌IP广告"))

    def test_owner_can_rename_asset_without_changing_original_extension(self):
        owner = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/uploads/甲/original.mp4",
                filename="original.mp4",
                media_type="video",
                ingest_source="oa_upload",
                uploaded_by_number="FD-A",
            )
            db.add(asset)
            db.commit()
            renamed = update_asset(asset.id, AssetUpdate(filename="水润面膜品牌创意广告"), db, owner)
            self.assertEqual(renamed.filename, "水润面膜品牌创意广告.mp4")
            self.assertEqual(renamed.object_key, "yxb/uploads/甲/original.mp4")
            with self.assertRaises(HTTPException) as raised:
                update_asset(asset.id, AssetUpdate(filename="不能改后缀.mov"), db, owner)
            self.assertEqual(raised.exception.status_code, 422)

    def test_asset_library_filter_only_returns_selected_library(self):
        user = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            db.add_all([
                Asset(object_key="yxb/source/a.mp4", filename="原片.mp4", media_type="video", library_type="source", asset_subtype="明星信息流原片"),
                Asset(object_key="yxb/remix/b.mp4", filename="混剪.mp4", media_type="video", library_type="remix", asset_subtype="其他混剪成片"),
            ])
            db.commit()
            page = list_assets(
                q="", category="", content_type="", status="", media_type="", library_type="remix",
                asset_subtype="", ingest_source="", directory="", favorite=None, sort="newest",
                page=1, page_size=24, db=db, user=user,
            )
            self.assertEqual(page.total, 1)
            self.assertEqual(page.items[0].library_type, "remix")

    def test_effective_material_mark_is_shared_filterable_and_reversible(self):
        marker = {"number": "FD-A", "realName": "标记同事", "groupName": "品牌营销部-品牌创意中心"}
        reviewer = {"number": "FD-B", "realName": "视频同事", "center": "视频中心"}
        viewer = {"number": "FD-C", "realName": "查看同事", "groupName": "品牌营销部-AI营销中心"}
        with SessionLocal() as db:
            video = Asset(
                object_key="yxb/source/effective.mp4",
                filename="有效一创.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
            )
            image = Asset(
                object_key="yxb/product/effective.jpg",
                filename="商品图.jpg",
                media_type="image",
                asset_scope="product_image",
            )
            db.add_all([video, image])
            db.commit()

            marked = set_asset_effective(
                video.id, AssetEffectiveUpdate(effective=True), db, marker
            )
            self.assertTrue(marked.effective)
            self.assertEqual(marked.effective_marked_by_name, "标记同事")

            page = list_assets(
                q="", category="", content_type="", status="", media_type="video",
                asset_scope="marketing_video", library_type="", asset_subtype="",
                folder_name="", ingest_source="", mine_only=False, directory="",
                favorite=None, hot_only=False, effective_only=True, sort="newest",
                page=1, page_size=24, db=db, user=viewer,
            )
            self.assertEqual(page.total, 1)
            self.assertEqual(page.items[0].filename, "有效一创.mp4")
            self.assertTrue(page.items[0].effective)

            with self.assertRaises(HTTPException) as denied:
                set_asset_effective(
                    video.id, AssetEffectiveUpdate(effective=False), db, viewer
                )
            self.assertEqual(denied.exception.status_code, 403)
            self.assertTrue(db.get(AssetEffectiveMark, video.id))

            unmarked = set_asset_effective(
                video.id, AssetEffectiveUpdate(effective=False), db, reviewer
            )
            self.assertFalse(unmarked.effective)
            empty_page = list_assets(
                q="", category="", content_type="", status="", media_type="video",
                asset_scope="marketing_video", library_type="", asset_subtype="",
                folder_name="", ingest_source="", mine_only=False, directory="",
                favorite=None, hot_only=False, effective_only=True, sort="newest",
                page=1, page_size=24, db=db, user=marker,
            )
            self.assertEqual(empty_page.total, 0)
            with self.assertRaises(HTTPException) as raised:
                set_asset_effective(
                    image.id, AssetEffectiveUpdate(effective=True), db, marker
                )
            self.assertEqual(raised.exception.status_code, 422)

    def test_effective_material_permission_uses_oa_center_fields_without_admin_bypass(self):
        self.assertTrue(can_mark_effective_asset({"groupName": "品牌营销部-品牌创意中心"}))
        self.assertTrue(can_mark_effective_asset({"deptName": "视频中心"}))
        self.assertTrue(can_mark_effective_asset({"center": "视频中心-素材制作组"}))
        self.assertFalse(can_mark_effective_asset({"groupName": "品牌营销部-AI营销中心"}))
        self.assertFalse(can_mark_effective_asset({"number": "FD-026222", "realName": "舒豪", "groupName": "AI营销中心"}))

    def test_effective_material_workstation_payload_and_clip_import_are_authoritative(self):
        marker = {"number": "FD-A", "realName": "标记同事", "groupName": "品牌营销部-品牌创意中心"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/source/effective-clip.mp4",
                filename="有效切片来源.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
                category="隐形水润面膜",
            )
            db.add(asset)
            db.commit()
            set_asset_effective(asset.id, AssetEffectiveUpdate(effective=True), db, marker)

            page = workstation_assets(
                q="", category="", folder_name="", library_type="source",
                effective_only=True, page=1, page_size=24, db=db, _service={},
            )
            self.assertEqual(page["total"], 1)
            self.assertTrue(page["items"][0]["effective"])
            self.assertTrue(page["items"][0]["effective_marked_at"])

            background = BackgroundTasks()
            fake_settings = SimpleNamespace(
                remix_worker_base_url="https://remix.example.test",
                remix_worker_token="x" * 32,
            )
            with patch("app.main.settings", fake_settings):
                queued = import_effective_asset_to_clip_library(
                    asset.id, background, db, marker
                )
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(len(background.tasks), 1)
            audit = db.scalar(
                select(AssetAuditLog).where(
                    AssetAuditLog.asset_id == asset.id,
                    AssetAuditLog.action == "queue_effective_clip_import",
                )
            )
            self.assertIsNotNone(audit)

            with patch("app.main._remix_worker_request") as request_mock:
                request_mock.return_value = {
                    "status": {"assetId": asset.id, "state": "approved"}
                }
                status = effective_asset_clip_library_status(asset.id, db, marker)
            self.assertEqual(status["status"]["state"], "approved")

    def test_effective_clip_import_blocks_unclassified_or_unmarked_assets(self):
        user = {"number": "FD-A", "realName": "同事", "groupName": "品牌营销部-品牌创意中心"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/source/pending.mp4",
                filename="待分类.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
                category="待分类",
            )
            db.add(asset)
            db.commit()
            with self.assertRaises(HTTPException) as unmarked:
                import_effective_asset_to_clip_library(
                    asset.id, BackgroundTasks(), db, user
                )
            self.assertEqual(unmarked.exception.status_code, 409)
            set_asset_effective(asset.id, AssetEffectiveUpdate(effective=True), db, user)
            with self.assertRaises(HTTPException) as unclassified:
                import_effective_asset_to_clip_library(
                    asset.id, BackgroundTasks(), db, user
                )
            self.assertEqual(unclassified.exception.status_code, 409)

    def test_platform_gmv_only_shows_returned_fields_and_keeps_real_zero(self):
        viewer = {"number": "FD-VIEW", "realName": "查看同事", "groupName": "品牌营销部"}
        synced_at = datetime(2026, 8, 21, 2, 0)
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/source/platform-gmv.mp4",
                filename="三平台成交额.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
            )
            missing_asset = Asset(
                object_key="yxb/source/no-platform-gmv.mp4",
                filename="未回传成交额.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
            )
            db.add_all([asset, missing_asset])
            db.flush()
            db.add(ChannelsAccount(
                id="channels-account",
                owner_number="FD-A",
                nickname="测试视频号",
                auth_file="tests/channels-account.json",
            ))
            db.flush()
            qianchuan = QianchuanDelivery(
                id="gmv-qianchuan",
                batch_id="gmv",
                asset_id=asset.id,
                created_by_number="FD-A",
                advertiser_id="qc-account",
                plan_id="qc-plan",
            )
            adq = AdqDelivery(
                id="gmv-adq",
                batch_id="gmv",
                asset_id=asset.id,
                created_by_number="FD-A",
                account_id="adq-account",
                adgroup_id="adq-unit",
            )
            channels = ChannelsDelivery(
                id="gmv-channels",
                batch_id="gmv",
                asset_id=asset.id,
                created_by_number="FD-A",
                account_id="channels-account",
                platform_content_id="channels-content",
            )
            db.add_all([qianchuan, adq, channels])
            db.flush()
            db.add_all([
                QianchuanMetricDaily(
                    task_id=qianchuan.id,
                    advertiser_id=qianchuan.advertiser_id,
                    plan_id=qianchuan.plan_id,
                    video_id="qc-video",
                    stat_date="2026-08-20",
                    metrics={"pay_order_amount": 123.45},
                    status="success",
                    has_data=True,
                    link_verified=True,
                    updated_at=synced_at,
                ),
                AdqMetricDaily(
                    task_id=adq.id,
                    account_id=adq.account_id,
                    adgroup_id=adq.adgroup_id,
                    video_id="adq-video",
                    stat_date="2026-08-20",
                    metrics={"order_amount_yuan": 0},
                    status="success",
                    has_data=True,
                    updated_at=synced_at,
                ),
                ChannelsMetricDaily(
                    delivery_id=channels.id,
                    account_id=channels.account_id,
                    data_date="2026-08-20",
                    gmv_fen=1990,
                    collected_at=synced_at,
                ),
            ])
            db.commit()

            summary = _asset_platform_gmv_summary(db, [asset.id, missing_asset.id])
            values = {item["platform"]: item["gmv_yuan"] for item in summary[asset.id]}
            self.assertEqual(values, {"qianchuan": 123.45, "adq": 0.0, "channels": 19.9})
            self.assertNotIn(missing_asset.id, summary)

            page = list_assets(
                q="三平台", category="", content_type="", status="", media_type="video",
                asset_scope="marketing_video", library_type="", asset_subtype="",
                folder_name="", ingest_source="", mine_only=False, directory="",
                favorite=None, hot_only=False, effective_only=False, sort="newest",
                page=1, page_size=24, db=db, user=viewer,
            )
            self.assertEqual(page.total, 1)
            self.assertEqual(
                {item.platform: item.gmv_yuan for item in page.items[0].platform_gmv},
                values,
            )

    def test_my_uploads_filter_is_private_to_current_oa_user(self):
        current_user = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            db.add_all([
                Asset(object_key="yxb/uploads/甲/a.mp4", filename="甲上传.mp4", media_type="video", uploaded_by_number="FD-A"),
                Asset(object_key="yxb/uploads/乙/b.mp4", filename="乙上传.mp4", media_type="video", uploaded_by_number="FD-B"),
                Asset(object_key="yxb/library/c.mp4", filename="OSS素材.mp4", media_type="video", uploaded_by_number=""),
            ])
            db.commit()
            page = list_assets(
                q="", category="", content_type="", status="", media_type="", asset_scope="",
                library_type="", asset_subtype="", ingest_source="", mine_only=True, directory="",
                favorite=None, hot_only=False, sort="newest", page=1, page_size=24, db=db, user=current_user,
            )
            self.assertEqual(page.total, 1)
            self.assertEqual(page.items[0].filename, "甲上传.mp4")

    def test_asset_search_matches_uploader_name_and_number(self):
        viewer = {"number": "FD-VIEW", "realName": "查看人", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            db.add_all([
                Asset(object_key="yxb/uploads/a.mp4", filename="普通文件.mp4", media_type="video", uploaded_by_name="叶森莹", uploaded_by_number="FD-1001"),
                Asset(object_key="yxb/uploads/b.mp4", filename="另一文件.mp4", media_type="video", uploaded_by_name="朱嘉琳", uploaded_by_number="FD-1002"),
            ])
            db.commit()
            common = dict(
                category="", content_type="", status="", media_type="", asset_scope="",
                library_type="", asset_subtype="", ingest_source="", mine_only=False,
                directory="", favorite=None, hot_only=False, sort="newest", page=1, page_size=24,
                db=db, user=viewer,
            )
            by_name = list_assets(q="叶森莹", **common)
            by_number = list_assets(q="FD-1002", **common)
            self.assertEqual([item.filename for item in by_name.items], ["普通文件.mp4"])
            self.assertEqual([item.filename for item in by_number.items], ["另一文件.mp4"])

    def test_chinese_json_tags_are_searchable_in_library_workstation_and_trash(self):
        viewer = {"number": "FD-VIEW", "realName": "查看人", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            active = Asset(
                object_key="yxb/source/star.mp4",
                filename="无人物名原片.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
                asset_subtype="明星信息流原片",
                tags=["上传素材", "李颖"],
            )
            deleted = Asset(
                object_key="yxb/source/deleted.mp4",
                filename="已删除原片.mp4",
                media_type="video",
                uploaded_by_number="FD-VIEW",
                tags=["李颖"],
                deleted_at=datetime.utcnow(),
            )
            db.add_all([active, deleted])
            db.commit()

            page = list_assets(
                q="李颖", category="", content_type="", status="", media_type="",
                asset_scope="marketing_video", library_type="source", asset_subtype="明星信息流原片",
                folder_name="", ingest_source="", mine_only=False, directory="", favorite=None,
                hot_only=False, sort="newest", page=1, page_size=24, db=db, user=viewer,
            )
            workstation = workstation_assets(
                q="李颖", category="", folder_name="", library_type="source",
                page=1, page_size=24, db=db, _service={},
            )
            trash = list_trash("李颖", "deleted", 1, 60, db, viewer)

            self.assertEqual([item.filename for item in page.items], ["无人物名原片.mp4"])
            self.assertEqual([item["filename"] for item in workstation["items"]], ["无人物名原片.mp4"])
            self.assertEqual([item.filename for item in trash.items], ["已删除原片.mp4"])

    def test_batch_delete_is_atomic_and_limited_to_manageable_assets(self):
        owner = {"number": "FD-A", "realName": "甲", "groupName": "品牌营销部", "deptJobName": "剪辑师"}
        with SessionLocal() as db:
            first = Asset(object_key="yxb/uploads/甲/a.mp4", filename="a.mp4", media_type="video", ingest_source="oa_upload", uploaded_by_number="FD-A")
            second = Asset(object_key="yxb/uploads/甲/b.mp4", filename="b.mp4", media_type="video", ingest_source="oa_upload", uploaded_by_number="FD-A")
            foreign = Asset(object_key="yxb/uploads/乙/c.mp4", filename="c.mp4", media_type="video", ingest_source="oa_upload", uploaded_by_number="FD-B")
            db.add_all([first, second, foreign])
            db.commit()
            ids = [first.id, second.id]
            result = batch_delete_assets(BatchDeletePayload(asset_ids=ids), db, owner)
            self.assertEqual(result["count"], 2)
            self.assertTrue(all(db.get(Asset, asset_id).deleted_at is not None for asset_id in ids))
            with self.assertRaises(HTTPException) as blocked:
                batch_delete_assets(BatchDeletePayload(asset_ids=[foreign.id]), db, owner)
            self.assertEqual(blocked.exception.status_code, 403)
            self.assertIsNone(db.get(Asset, foreign.id).deleted_at)

    def test_favorites_are_private_per_oa_number(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/a.mp4", filename="a.mp4", media_type="video")
            db.add(asset)
            db.commit()
            db.add(AssetFavorite(user_number="FD-A", asset_id=asset.id))
            db.commit()
            self.assertEqual(favorite_asset_ids(db, {"number": "FD-A"}), {asset.id})
            self.assertEqual(favorite_asset_ids(db, {"number": "FD-B"}), set())

    def test_qianchuan_tasks_and_metrics_are_private_per_oa_number(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/private/a.mp4", filename="a.mp4", media_type="video")
            db.add(asset)
            db.commit()
            for suffix, owner in (("1", "FD-A"), ("2", "FD-B")):
                db.add(
                    QianchuanDelivery(
                        id=f"00000000-0000-0000-0000-00000000000{suffix}",
                        batch_id=f"batch-{suffix}",
                        asset_id=asset.id,
                        created_by_number=owner,
                        created_by_name=owner,
                        advertiser_id=f"10{suffix}",
                        plan_id=f"20{suffix}",
                        plan_type="multiplication",
                        platform_asset_id=f"video-{suffix}",
                        binding_verified_at=datetime.utcnow(),
                        status="success",
                    )
                )
            db.commit()
            visible = qianchuan_tasks(None, 100, db, {"number": "FD-A"})["items"]
            self.assertEqual([item["created_by_number"] for item in visible], ["FD-A"])
            background = BackgroundTasks()
            result = qianchuan_metrics_sync(
                QianchuanMetricsSync(), background, db, {"number": "FD-A"}
            )
            self.assertEqual(result["task_ids"], ["00000000-0000-0000-0000-000000000001"])

    def test_qianchuan_manual_metrics_is_bounded_and_deduplicated(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/metrics/manual.mp4", filename="manual.mp4", media_type="video")
            db.add(asset)
            db.commit()
            for index in range(55):
                db.add(QianchuanDelivery(
                    id=f"manual-{index:02d}",
                    batch_id="manual",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2001",
                    plan_type="multiplication",
                    platform_asset_id=f"video-{index}",
                    binding_verified_at=datetime.utcnow(),
                    status="success",
                    updated_at=datetime.utcnow() + timedelta(seconds=index),
                ))
            db.commit()
            background = BackgroundTasks()
            first = qianchuan_metrics_sync(QianchuanMetricsSync(), background, db, owner)
            second = qianchuan_metrics_sync(QianchuanMetricsSync(), background, db, owner)

        self.assertEqual(first["status"], "queued")
        self.assertEqual(len(first["task_ids"]), settings.qianchuan_metrics_manual_batch_size)
        self.assertEqual(second["status"], "already_running")
        self.assertEqual(len(background.tasks), 1)

    def test_qianchuan_metrics_selects_only_stale_stable_records(self):
        current = datetime.utcnow()
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/metrics/auto.mp4", filename="auto.mp4", media_type="video")
            db.add(asset)
            db.commit()
            for index in range(35):
                is_fresh = index < 5
                db.add(QianchuanDelivery(
                    id=f"auto-{index:02d}",
                    batch_id="auto",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    platform_asset_id=f"auto-video-{index}",
                    status="success",
                    metrics_synced_at=current - (timedelta(minutes=30) if is_fresh else timedelta(hours=3, minutes=index)),
                ))
            db.commit()
            selected = select_qianchuan_metrics_task_ids(db, limit=30, now=current)

        self.assertEqual(len(selected), 30)
        self.assertFalse(any(task_id in selected for task_id in {f"auto-{index:02d}" for index in range(5)}))

    def test_qianchuan_auto_metrics_continues_while_delivery_is_active(self):
        current = datetime.utcnow()
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/metrics/defer.mp4", filename="defer.mp4", media_type="video")
            db.add(asset)
            db.commit()
            db.add_all([
                QianchuanDelivery(id="defer-active", batch_id="defer", asset_id=asset.id, created_by_number="FD-A", advertiser_id="1001", status="pending"),
                QianchuanDelivery(id="defer-metric", batch_id="defer", asset_id=asset.id, created_by_number="FD-A", advertiser_id="1001", platform_asset_id="video-ready", status="success"),
            ])
            db.commit()

        result = run_qianchuan_auto_metrics_if_due(now=current)
        self.assertEqual(result["status"], "completed")
        self.assertIn("defer-metric", result["task_ids"])

    def test_qianchuan_auto_metrics_runs_only_one_bounded_batch(self):
        current = datetime.utcnow()
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/metrics/due.mp4", filename="due.mp4", media_type="video")
            db.add(asset)
            db.commit()
            for index in range(35):
                db.add(QianchuanDelivery(
                    id=f"due-{index:02d}",
                    batch_id="due",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    platform_asset_id=f"due-video-{index}",
                    status="success",
                ))
            db.commit()

        with patch("app.main._run_qianchuan_metrics_serial") as sync:
            result = run_qianchuan_auto_metrics_if_due(now=current)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["task_ids"]), settings.qianchuan_metrics_auto_batch_size)
        sync.assert_called_once_with(result["task_ids"], None, None)
        with SessionLocal() as db:
            self.assertTrue(db.get(AppMeta, "qianchuan.metrics.auto.last_run_at").value)

    def test_qianchuan_metrics_uses_a_lane_separate_from_delivery(self):
        self.assertIsNot(_qianchuan_metrics_slot, _qianchuan_job_slots)

    def test_daily_metrics_reads_each_plan_once_and_fans_out_to_private_records(self):
        stat_date = date(2026, 8, 9)
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/daily/shared.mp4", filename="shared.mp4", media_type="video")
            db.add(asset)
            db.commit()
            tasks = []
            for index, owner in enumerate(("FD-A", "FD-B"), start=1):
                task = QianchuanDelivery(
                    id=f"daily-shared-{index}",
                    batch_id="daily",
                    asset_id=asset.id,
                    created_by_number=owner,
                    advertiser_id="1001",
                    plan_id="2001",
                    plan_type="multiplication",
                    platform_asset_id="video-shared",
                    binding_verified_at=datetime(2026, 8, 8),
                    status="success",
                    created_at=datetime(2026, 8, 8),
                )
                db.add(task)
                tasks.append(task)
            db.commit()
            tasks = _daily_metric_tasks(db, stat_date=stat_date)

        plan_result = {
            "request_id": "daily-request",
            "videos": [{
                "video_id": "video-shared",
                "material_status": "DELIVERY_OK",
                "needs_reactivation": False,
                "stats_info": {
                    "stat_cost_for_roi2": 12.5,
                    "total_pay_order_gmv_include_coupon_for_roi2": 25,
                    "total_pay_order_count_for_roi2": 1,
                    "product_show_count_for_roi2": 100,
                    "product_click_count_for_roi2": 4,
                },
            }],
        }
        with patch("app.main._read_plan_daily_with_retry", return_value=plan_result) as read:
            result = _run_daily_metric_date(stat_date, tasks)

        self.assertEqual(read.call_count, 1)
        self.assertEqual(result["task_count"], 2)
        self.assertEqual(result["group_count"], 1)
        with SessionLocal() as db:
            rows = db.scalars(select(QianchuanMetricDaily)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row.task_id for row in rows}, {"daily-shared-1", "daily-shared-2"})
            self.assertTrue(all(row.metrics["stat_cost"] == 12.5 for row in rows))

    def test_daily_metrics_falls_back_when_optional_plan_fields_are_rejected(self):
        reduced_result = {"videos": [], "request_id": "fallback-request", "pages": 1}
        with SessionLocal() as db, patch(
            "app.main.qianchuan_service.plan_material_videos",
            side_effect=[
                QianchuanError("参数错误", code=40000, request_id="bad-fields"),
                reduced_result,
            ],
        ) as read:
            result = _read_plan_daily_with_retry(db, "1001", "2001", date(2026, 8, 9))

        self.assertEqual(read.call_count, 2)
        first_fields = read.call_args_list[0].kwargs["fields"]
        second_fields = read.call_args_list[1].kwargs["fields"]
        self.assertIn("product_show_count_for_roi2", first_fields)
        self.assertNotIn("product_show_count_for_roi2", second_fields)
        self.assertNotIn("product_click_count_for_roi2", second_fields)
        self.assertIn("核心经营数据", result["field_warning"])

    def test_daily_summary_distinguishes_true_zero_from_not_refreshed(self):
        target = datetime.now(timezone.utc).date() - timedelta(days=1)
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/daily/zero.mp4", filename="zero.mp4", media_type="video")
            db.add(asset)
            db.commit()
            task = QianchuanDelivery(
                id="daily-zero",
                batch_id="daily",
                asset_id=asset.id,
                created_by_number="FD-A",
                advertiser_id="1001",
                plan_id="2001",
                plan_type="multiplication",
                platform_asset_id="video-zero",
                binding_verified_at=datetime.utcnow() - timedelta(days=2),
                status="success",
                created_at=datetime.utcnow() - timedelta(days=2),
            )
            db.add(task)
            db.commit()
            pending = _task_daily_summary(db, task, window_end=target)
            db.add(QianchuanMetricDaily(
                task_id=task.id,
                advertiser_id=task.advertiser_id,
                plan_id=task.plan_id,
                video_id=task.platform_asset_id,
                stat_date=target.isoformat(),
                status="success",
                has_data=True,
                link_verified=True,
                metrics={"stat_cost": 0, "pay_order_amount": 0, "pay_order_count": 0, "show_cnt": 0},
            ))
            db.commit()
            zero = _task_daily_summary(db, task, window_end=target)
            visible = qianchuan_tasks(None, None, db, {"number": "FD-A", "realName": "甲"}, page=1, page_size=10)

        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["metrics"], {})
        self.assertIn(zero["status"], {"fresh", "partial"})
        self.assertEqual(zero["metrics"]["stat_cost"], 0)
        self.assertEqual(zero["metrics"]["prepay_and_pay_order_roi"], 0)
        self.assertTrue(zero["link_verified"])
        self.assertEqual(visible["items"][0]["metrics_link_status"], "verified")

    def test_daily_auto_retries_recent_error_dates_after_daily_completion(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        target = date(2026, 8, 9)
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/daily/retry.mp4", filename="retry.mp4", media_type="video")
            db.add(asset)
            db.commit()
            task = QianchuanDelivery(
                id="daily-retry-error",
                batch_id="daily",
                asset_id=asset.id,
                created_by_number="FD-A",
                advertiser_id="1001",
                plan_id="2001",
                plan_type="multiplication",
                platform_asset_id="video-retry",
                binding_verified_at=datetime(2026, 8, 8),
                status="success",
                created_at=datetime(2026, 8, 8),
            )
            db.add(task)
            db.add(QianchuanMetricDaily(
                task_id=task.id,
                advertiser_id=task.advertiser_id,
                plan_id=task.plan_id,
                video_id=task.platform_asset_id,
                stat_date=target.isoformat(),
                status="error",
                error_code="40100",
            ))
            db.add(AppMeta(key="qianchuan.metrics.daily.last_completed_date", value=target.isoformat()))
            db.commit()

        seen = []
        def complete(stat_date, tasks, **_kwargs):
            seen.append((stat_date, [task.id for task in tasks]))
            return {"task_count": len(tasks), "group_count": 1, "error_groups": 0}

        with patch("app.main._run_daily_metric_date", side_effect=complete):
            result = run_qianchuan_daily_metrics_if_due(now=now)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["dates"], [target.isoformat()])
        self.assertEqual(seen, [(target, ["daily-retry-error"])])

    def test_daily_auto_backfill_covers_all_owners_without_per_record_api_reads(self):
        now = datetime(2026, 8, 10, 2, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/daily/all.mp4", filename="all.mp4", media_type="video")
            db.add(asset)
            db.commit()
            for index, owner in enumerate(("FD-A", "FD-B"), start=1):
                db.add(QianchuanDelivery(
                    id=f"daily-all-{index}",
                    batch_id="daily",
                    asset_id=asset.id,
                    created_by_number=owner,
                    advertiser_id="1001",
                    plan_id="2001",
                    plan_type="multiplication",
                    platform_asset_id=f"video-{index}",
                    binding_verified_at=datetime(2026, 8, 8),
                    status="success",
                    created_at=datetime(2026, 8, 8),
                ))
            db.commit()

        outcomes = []
        def complete(_stat_date, tasks, **_kwargs):
            outcomes.append({task.created_by_number for task in tasks})
            return {"task_count": len(tasks), "group_count": 1 if tasks else 0, "error_groups": 0}

        with patch("app.main._run_daily_metric_date", side_effect=complete):
            result = run_qianchuan_daily_metrics_if_due(now=now)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(owners == {"FD-A", "FD-B"} for owners in outcomes))
        self.assertEqual(len(result["dates"]), settings.qianchuan_metrics_daily_lookback_days)

    @patch.object(qianchuan_service, "dispatch_block_reason", return_value="")
    def test_qianchuan_maintenance_hold_blocks_same_asset_account_only(self, _auth_gate):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/queue/hold.mp4", filename="hold.mp4", media_type="video")
            db.add(asset)
            db.flush()
            db.add_all([
                QianchuanDelivery(id="held", batch_id="hold", asset_id=asset.id, created_by_number="FD-A", advertiser_id="1001", status="maintenance_hold"),
                QianchuanDelivery(id="same-pair", batch_id="hold", asset_id=asset.id, created_by_number="FD-A", advertiser_id="1001", status="pending"),
                QianchuanDelivery(id="other-account", batch_id="hold", asset_id=asset.id, created_by_number="FD-A", advertiser_id="1002", status="pending"),
            ])
            db.commit()
        self.assertEqual(claim_qianchuan_tasks(3), ["other-account"])
        with SessionLocal() as db:
            self.assertEqual(db.get(QianchuanDelivery, "held").status, "maintenance_hold")
            self.assertEqual(db.get(QianchuanDelivery, "same-pair").status, "pending")

    @patch.object(qianchuan_service, "dispatch_block_reason", return_value="")
    def test_qianchuan_queue_claims_three_lanes_without_parallel_duplicate_uploads(self, _auth_gate):
        with SessionLocal() as db:
            first = Asset(object_key="yxb/queue/a.mp4", filename="a.mp4", media_type="video")
            second = Asset(object_key="yxb/queue/b.mp4", filename="b.mp4", media_type="video")
            db.add_all([first, second])
            db.commit()
            first_id, second_id = first.id, second.id
            tasks = [
                QianchuanDelivery(id="queue-a-1", batch_id="queue", asset_id=first_id, created_by_number="FD-A", advertiser_id="1001", plan_id="2001", status="pending"),
                QianchuanDelivery(id="queue-a-2", batch_id="queue", asset_id=first_id, created_by_number="FD-A", advertiser_id="1001", plan_id="2002", status="pending"),
                QianchuanDelivery(id="queue-b-1", batch_id="queue", asset_id=second_id, created_by_number="FD-B", advertiser_id="1001", plan_id="2003", status="pending"),
            ]
            db.add_all(tasks)
            db.commit()

        claimed = claim_qianchuan_tasks(3)
        self.assertEqual(len(claimed), 2)
        with SessionLocal() as db:
            claimed_tasks = db.scalars(select(QianchuanDelivery).where(QianchuanDelivery.id.in_(claimed))).all()
            self.assertEqual({task.asset_id for task in claimed_tasks}, {first_id, second_id})
            self.assertEqual({task.status for task in claimed_tasks}, {"uploading"})
            waiting = db.get(QianchuanDelivery, "queue-a-2")
            self.assertEqual(waiting.status, "pending")

    def test_qianchuan_records_support_five_ten_fifty_and_hundred_item_pagination(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/pages/a.mp4", filename="a.mp4", media_type="video")
            db.add(asset)
            db.commit()
            for index in range(62):
                db.add(QianchuanDelivery(
                    id=f"page-{index:02d}",
                    batch_id="pages",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id=str(2000 + index),
                    status="success",
                    created_at=datetime.utcnow() + timedelta(seconds=index),
                ))
            db.commit()
            second_page = qianchuan_tasks(None, None, db, owner, page=2, page_size=5)
            first_ten = qianchuan_tasks(None, None, db, owner, page=1, page_size=10)
            first_fifty = qianchuan_tasks(None, None, db, owner, page=1, page_size=50)
            first_hundred = qianchuan_tasks(None, None, db, owner, page=1, page_size=100)

        self.assertEqual(second_page["total"], 62)
        self.assertEqual(second_page["total_pages"], 13)
        self.assertEqual(len(second_page["items"]), 5)
        self.assertEqual(first_ten["total_pages"], 7)
        self.assertEqual(len(first_ten["items"]), 10)
        self.assertEqual(first_fifty["total_pages"], 2)
        self.assertEqual(len(first_fifty["items"]), 50)
        self.assertEqual(first_hundred["total_pages"], 1)
        self.assertEqual(len(first_hundred["items"]), 62)

    def test_qianchuan_plan_materials_reads_inventory_metrics_and_wis_link(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/plan/inventory.mp4", filename="计划素材.mp4", media_type="video")
            db.add(asset)
            db.commit()
            db.add(QianchuanDelivery(
                id="inventory-task",
                batch_id="inventory",
                asset_id=asset.id,
                created_by_number="FD-A",
                advertiser_id="1001",
                plan_id="2001",
                platform_asset_id="video-1",
                status="success",
            ))
            db.commit()
            rows = {
                "videos": [
                    {
                        "video_id": "video-1",
                        "material_id": "material-1",
                        "title": "计划素材",
                        "material_status": "DELIVERY_OK",
                        "delivery_ready": True,
                        "needs_reactivation": False,
                        "is_delete": False,
                        "stats_info": {
                            "stat_cost_for_roi2": 100,
                            "total_pay_order_gmv_include_coupon_for_roi2": 250,
                            "total_pay_order_count_for_roi2": 3,
                        },
                    },
                    {
                        "video_id": "video-2",
                        "material_id": "material-2",
                        "title": "已停用素材",
                        "material_status": "DELETED",
                        "delivery_ready": False,
                        "needs_reactivation": True,
                        "is_delete": True,
                        "stats_info": {},
                    },
                ],
                "request_id": "request-plan-materials",
                "pages": 1,
            }
            with patch.object(qianchuan_service, "plan_material_videos", return_value=rows):
                result = qianchuan_plan_materials(
                    "1001", "2001", "2026-08-01", "2026-08-07", db, owner,
                )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["inactive_count"], 1)
        self.assertEqual(result["items"][0]["metrics"]["stat_cost"], 100)
        self.assertEqual(result["items"][0]["metrics"]["pay_order_amount"], 250)
        self.assertEqual(result["items"][0]["metrics"]["prepay_and_pay_order_roi"], 2.5)
        self.assertEqual(result["items"][0]["linked_asset"]["asset_name"], "计划素材.mp4")
        self.assertFalse(result["items"][1]["active"])

    def test_qianchuan_batch_push_creates_one_private_task_per_asset_and_plan(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            assets = [
                Asset(object_key=f"yxb/batch/{name}.mp4", filename=f"{name}.mp4", media_type="video")
                for name in ("a", "b")
            ]
            db.add_all(assets)
            db.commit()
            payload = QianchuanPushCreate(
                asset_ids=[asset.id for asset in assets],
                targets=[
                    QianchuanTarget(advertiser_id="1001", plan_id="2001", plan_name="计划一", plan_type="multiplication"),
                    QianchuanTarget(advertiser_id="1001", plan_id="2002", plan_name="计划二", plan_type="multiplication"),
                ],
            )
            background = BackgroundTasks()
            with patch.object(
                qianchuan_service,
                "status",
                return_value={"authorized": True, "message": "授权可用"},
            ), patch.object(
                qianchuan_service,
                "resolve_plan_target",
                side_effect=lambda _db, **kwargs: {
                    "id": kwargs["plan_id"],
                    "name": f"计划{kwargs['plan_id']}",
                    "plan_type": "multiplication",
                    "can_attach_video": True,
                },
            ):
                result = qianchuan_push(payload, background, db, owner)

            self.assertEqual(result["asset_count"], 2)
            self.assertEqual(result["target_count"], 2)
            self.assertEqual(result["new_task_count"], 4)
            self.assertEqual(len(result["task_ids"]), 4)
            tasks = db.scalars(select(QianchuanDelivery).where(QianchuanDelivery.batch_id == result["batch_id"])).all()
            self.assertEqual(len(tasks), 4)
            self.assertEqual({task.asset_id for task in tasks}, {asset.id for asset in assets})
            self.assertEqual({task.plan_id for task in tasks}, {"2001", "2002"})
            self.assertEqual({task.created_by_number for task in tasks}, {"FD-A"})

    def test_workstation_qianchuan_catalog_matches_live_cloud_manager_directory(self):
        advertiser_id = "9918672250595328"
        plan_id = "9918702897946462"
        with SessionLocal() as db, patch.object(
            qianchuan_service,
            "validated_status",
            return_value={"authorized": True, "message": "千川授权可用"},
        ), patch.object(
            qianchuan_service,
            "accounts",
            return_value=[{"id": advertiser_id, "name": "营销部真实账户"}],
        ), patch.object(
            qianchuan_service,
            "plans",
            return_value={
                "items": [
                    {
                        "id": plan_id,
                        "name": "全品类可选计划",
                        "plan_type": "multiplication",
                        "can_attach_video": True,
                    }
                ],
                "counts": {"total": 1, "multiplication": 1},
                "warnings": [],
                "complete": True,
                "cached": False,
            },
        ):
            accounts = workstation_qianchuan_accounts(
                db=db,
                _service={"service": True},
            )
            plans = workstation_qianchuan_plans(
                advertiser_id=advertiser_id,
                q="",
                refresh=False,
                cached_only=False,
                scope="all",
                db=db,
                _service={"service": True},
            )
        self.assertEqual(accounts["items"][0]["id"], advertiser_id)
        self.assertEqual(plans["items"][0]["id"], plan_id)
        self.assertTrue(plans["items"][0]["can_attach_video"])
        self.assertEqual(plans["source"], "qianchuan live plan directory")

        product_plan_map = workstation_qianchuan_product_plan_map(
            _service={"service": True},
        )
        bundles = {item["key"]: item for item in product_plan_map["items"]}
        self.assertIn("black_crystal_mask", bundles)
        self.assertTrue(bundles["black_crystal_mask"]["rules"])
        self.assertEqual(product_plan_map["source"]["revision"], 536)

    def test_workstation_qianchuan_requires_trusted_review_and_is_idempotent(self):
        advertiser_id = "9918672250595328"
        plan_id = "9918702897946462"
        plan = {
            "id": plan_id,
            "name": "黑晶真实计划",
            "plan_type": "multiplication",
            "status": "DELIVERY_OK",
            "status_label": "投放中",
            "marketing_goal": "LIVE_PROM_GOODS",
            "can_attach_video": True,
        }
        target = {
            "advertiser_id": advertiser_id,
            "advertiser_name": "黑晶试点账户",
            "plan_id": plan_id,
            "plan_name": "",
            "plan_alias": "业务备注名",
            "plan_type": "multiplication",
        }
        with SessionLocal() as db, patch.object(
            qianchuan_service,
            "validated_status",
            return_value={"authorized": True, "message": "千川授权可用"},
        ), patch.object(
            qianchuan_service,
            "accounts",
            return_value=[{"id": advertiser_id, "name": "黑晶试点账户"}],
        ), patch.object(
            qianchuan_service,
            "resolve_plan_target",
            return_value=plan,
        ):
            verified = workstation_qianchuan_target_verify(
                advertiser_id=advertiser_id,
                plan_id=plan_id,
                plan_type="multiplication",
                db=db,
                _service={"service": True},
            )
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["plan"]["name"], "黑晶真实计划")

            asset = Asset(
                object_key="yxb/workstation/auto-black-1.mp4",
                filename="自动混剪-黑晶面膜-20260901-001.mp4",
                media_type="video",
                category="黑晶面膜",
                library_type="remix",
                asset_subtype="AI混剪成片",
                source="wis_remix_workstation",
            )
            db.add(asset)
            db.flush()
            assessment = {
                "status": "passed",
                "recommendation": "approve",
                "score": 100,
                "autoApproved": True,
                "checks": [
                    {"name": "文件可播放", "passed": True, "detail": "通过"},
                    {"name": "产品分类一致", "passed": True, "detail": "通过"},
                ],
            }
            provenance = {
                "automatic_review_enabled": True,
                "automatic_assessment": assessment,
            }
            db.add(
                WorkstationReturn(
                    idempotency_key="wis-remix:test:black:1",
                    object_key=asset.object_key,
                    asset_id=asset.id,
                    filename=asset.filename,
                    file_size=1024,
                    sha256="a" * 64,
                    mime_type="video/mp4",
                    status="completed",
                    provenance=provenance,
                    completed_at=datetime.utcnow(),
                )
            )
            _register_workstation_automatic_review(
                db,
                asset,
                provenance,
                actor_number="FD-AUTO",
                actor_name="自动混剪",
            )
            db.commit()

            payload = WorkstationQianchuanPushCreate(
                asset_id=asset.id,
                idempotency_key="wis-remix:test:black:delivery:1",
                product_category="黑晶面膜",
                actor_number="FD-AUTO",
                actor_name="自动混剪",
                enabled=True,
                confirmed=True,
                daily_material_limit=1,
                daily_spend_guard_yuan=100,
                target=target,
            )
            queued = workstation_qianchuan_push(
                payload,
                db=db,
                _service={"service": True},
            )
            repeated = workstation_qianchuan_push(
                payload,
                db=db,
                _service={"service": True},
            )
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(repeated["status"], "already_queued")
            self.assertEqual(queued["task"]["plan_id"], plan_id)
            readback = workstation_qianchuan_delivery(
                payload.idempotency_key,
                db=db,
                _service={"service": True},
            )
            self.assertEqual(readback["task"]["id"], queued["task"]["id"])

            first_task = db.get(QianchuanDelivery, queued["task"]["id"])
            first_task.status = "success"
            first_task.platform_asset_id = "verified-video-with-pending-metrics"
            db.add(first_task)
            second_asset = Asset(
                object_key="yxb/workstation/auto-black-2.mp4",
                filename="自动混剪-黑晶面膜-20260901-002.mp4",
                media_type="video",
                category="黑晶面膜",
                library_type="remix",
                asset_subtype="AI混剪成片",
                source="wis_remix_workstation",
            )
            db.add(second_asset)
            db.flush()
            second_provenance = {
                "automatic_review_enabled": True,
                "automatic_assessment": assessment,
            }
            db.add(
                WorkstationReturn(
                    idempotency_key="wis-remix:test:black:2",
                    object_key=second_asset.object_key,
                    asset_id=second_asset.id,
                    filename=second_asset.filename,
                    file_size=1024,
                    sha256="b" * 64,
                    mime_type="video/mp4",
                    status="completed",
                    provenance=second_provenance,
                    completed_at=datetime.utcnow(),
                )
            )
            _register_workstation_automatic_review(
                db,
                second_asset,
                second_provenance,
                actor_number="FD-AUTO",
                actor_name="自动混剪",
            )
            db.commit()
            second_payload = WorkstationQianchuanPushCreate(
                asset_id=second_asset.id,
                idempotency_key="wis-remix:test:black:delivery:2",
                product_category="黑晶面膜",
                actor_number="FD-AUTO",
                actor_name="自动混剪",
                enabled=True,
                confirmed=True,
                daily_material_limit=2,
                daily_spend_guard_yuan=100,
                target=target,
            )
            second_queued = workstation_qianchuan_push(
                second_payload,
                db=db,
                _service={"service": True},
            )
            self.assertEqual(second_queued["status"], "queued")
            self.assertEqual(second_queued["guard"]["spend_verification_status"], "pending")
            self.assertEqual(second_queued["guard"]["unverified_success_count"], 1)

    def test_workstation_qianchuan_refuses_live_push_without_spend_guard(self):
        payload = WorkstationQianchuanPushCreate(
            asset_id=1,
            idempotency_key="wis-remix:test:no-budget",
            product_category="黑晶面膜",
            actor_number="FD-AUTO",
            enabled=True,
            confirmed=True,
            daily_material_limit=1,
            daily_spend_guard_yuan=None,
            target={
                "advertiser_id": "8818672250595328",
                "plan_id": "8818702897946462",
                "plan_type": "multiplication",
            },
        )
        with SessionLocal() as db, patch.object(
            qianchuan_service,
            "validated_status",
            return_value={"authorized": True, "message": "千川授权可用"},
        ), patch.object(
            qianchuan_service,
            "accounts",
            return_value=[{"id": "8818672250595328", "name": "账户"}],
        ), patch.object(
            qianchuan_service,
            "resolve_plan_target",
            return_value={
                "id": "8818702897946462",
                "name": "计划",
                "plan_type": "multiplication",
                "can_attach_video": True,
            },
        ):
            with self.assertRaisesRegex(HTTPException, "每日消耗护栏"):
                workstation_qianchuan_push(
                    payload,
                    db=db,
                    _service={"service": True},
                )

    def test_qianchuan_plans_marks_latest_capacity_limit_red(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/full/a.mp4", filename="a.mp4", media_type="video")
            db.add(asset)
            db.flush()
            db.add(
                QianchuanDelivery(
                    id="full-plan-task",
                    batch_id="full-plan-batch",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2001",
                    plan_name="已满计划",
                    status="failed",
                    error_message="添加素材数量超过上限，请刷新后重试",
                    updated_at=datetime(2026, 8, 13, 10, 0),
                )
            )
            db.commit()
            source = {
                "items": [{
                    "id": "2001", "name": "已满计划", "plan_type": "multiplication",
                    "can_attach_video": True,
                }],
                "counts": {"total": 1, "multiplication": 1, "full_domain": 0, "standard": 0},
            }
            with patch.object(qianchuan_service, "plans", return_value=source):
                result = qianchuan_plans("1001", "", False, False, "all", db, {})

        self.assertTrue(result["items"][0]["is_full"])
        self.assertTrue(result["items"][0]["can_attach_video"])
        self.assertEqual(result["counts"]["full"], 1)
        self.assertIn("计划已满", result["items"][0]["capacity_message"])

    def test_qianchuan_later_success_clears_capacity_limit_evidence(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/full/b.mp4", filename="b.mp4", media_type="video")
            db.add(asset)
            db.flush()
            db.add_all([
                QianchuanDelivery(
                    id="old-full-plan-task", batch_id="full-plan-batch", asset_id=asset.id,
                    created_by_number="FD-A", advertiser_id="1001", plan_id="2002",
                    status="failed", error_message="添加素材数量超过上限",
                    updated_at=datetime(2026, 8, 13, 9, 0),
                ),
                QianchuanDelivery(
                    id="new-success-plan-task", batch_id="success-plan-batch", asset_id=asset.id,
                    created_by_number="FD-A", advertiser_id="1001", plan_id="2002",
                    status="success", updated_at=datetime(2026, 8, 13, 10, 0),
                ),
            ])
            db.commit()
            source = {
                "items": [{"id": "2002", "name": "已恢复计划", "plan_type": "multiplication", "can_attach_video": True}],
                "counts": {"total": 1, "multiplication": 1, "full_domain": 0, "standard": 0},
            }
            with patch.object(qianchuan_service, "plans", return_value=source):
                result = qianchuan_plans("1001", "", False, False, "all", db, {})

        self.assertFalse(result["items"][0]["is_full"])
        self.assertTrue(result["items"][0]["can_attach_video"])

    def test_qianchuan_delivery_filename_strips_only_internal_upload_prefix(self):
        service = QianchuanService()
        self.assertEqual(
            service._delivery_filename("20260813T090757Z-523409a7c7-0813-燕窝素材.mp4"),
            "0813-燕窝素材.mp4",
        )
        self.assertEqual(service._delivery_filename("正常-0813素材.mp4"), "正常-0813素材.mp4")

    def test_qianchuan_records_support_private_search_and_soft_delete(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/search/a.mp4", filename="黑晶测试.mp4", media_type="video")
            db.add(asset)
            db.commit()
            task = QianchuanDelivery(
                id="10000000-0000-0000-0000-000000000001",
                batch_id="batch-search",
                asset_id=asset.id,
                created_by_number="FD-A",
                created_by_name="甲",
                advertiser_id="1001",
                advertiser_name="WIS 官旗",
                plan_id="2001",
                plan_name="黑晶乘方计划",
                status="success",
            )
            db.add(task)
            db.commit()

            result = qianchuan_tasks(None, 100, db, owner, q="乘方", status="success")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["id"], task.id)

            deleted = qianchuan_task_delete(task.id, db, owner)
            self.assertTrue(deleted["ok"])
            self.assertEqual(qianchuan_tasks(None, 100, db, owner)["total"], 0)
            self.assertIsNotNone(db.get(QianchuanDelivery, task.id).deleted_at)
            with self.assertRaises(HTTPException) as missing:
                qianchuan_task_delete(task.id, db, {"number": "FD-B"})
            self.assertEqual(missing.exception.status_code, 404)

    def test_qianchuan_batch_retry_only_requeues_owned_failed_targets(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/retry/original.mp4",
                filename="original.mp4",
                media_type="video",
            )
            db.add(asset)
            db.commit()
            db.add_all([
                QianchuanDelivery(
                    id="retry-success",
                    batch_id="batch-retry",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2001",
                    plan_type="multiplication",
                    platform_asset_id="video-success",
                    status="success",
                    message="已成功",
                ),
                QianchuanDelivery(
                    id="retry-upload",
                    batch_id="batch-retry",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2002",
                    plan_type="multiplication",
                    upload_task_id="terminal-upload-task",
                    status="failed",
                    failure_stage="upload",
                    last_error_category="async_upload_failed",
                    error_message="远端上传任务失败",
                ),
                QianchuanDelivery(
                    id="retry-binding",
                    batch_id="batch-retry",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2003",
                    plan_type="multiplication",
                    platform_asset_id="video-ready",
                    upload_task_id="completed-upload-task",
                    status="partial",
                    failure_stage="plan_binding",
                    last_error_category="plan_verification_pending",
                    error_message="计划回读延迟",
                ),
                QianchuanDelivery(
                    id="retry-standard",
                    batch_id="batch-retry",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2004",
                    plan_type="standard",
                    platform_asset_id="video-standard",
                    status="partial",
                ),
                QianchuanDelivery(
                    id="retry-pending",
                    batch_id="batch-retry",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2005",
                    plan_type="multiplication",
                    status="pending",
                ),
                QianchuanDelivery(
                    id="retry-other-owner",
                    batch_id="batch-retry",
                    asset_id=asset.id,
                    created_by_number="FD-B",
                    advertiser_id="1001",
                    plan_id="2006",
                    plan_type="multiplication",
                    status="failed",
                ),
            ])
            db.commit()

            result = qianchuan_batch_retry_failed(
                "batch-retry", BackgroundTasks(), db, owner
            )

            self.assertEqual(result["queued_count"], 2)
            self.assertEqual(
                set(result["queued_task_ids"]), {"retry-upload", "retry-binding"}
            )
            self.assertEqual(result["preserved_success_count"], 1)
            self.assertEqual(result["skipped_count"], 2)

            upload = db.get(QianchuanDelivery, "retry-upload")
            self.assertEqual(upload.status, "pending")
            self.assertEqual(upload.upload_task_id, "")
            self.assertIn("重新上传原视频", upload.message)
            self.assertEqual(upload.error_message, "")

            binding = db.get(QianchuanDelivery, "retry-binding")
            self.assertEqual(binding.status, "pending")
            self.assertEqual(binding.platform_asset_id, "video-ready")
            self.assertEqual(binding.upload_task_id, "completed-upload-task")
            self.assertIn("复用已上传原视频", binding.message)

            self.assertEqual(db.get(QianchuanDelivery, "retry-success").status, "success")
            self.assertEqual(db.get(QianchuanDelivery, "retry-standard").status, "partial")
            self.assertEqual(db.get(QianchuanDelivery, "retry-pending").status, "pending")
            self.assertEqual(db.get(QianchuanDelivery, "retry-other-owner").status, "failed")

    def test_qianchuan_single_retry_keeps_a_live_async_upload_task(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/retry/live.mp4",
                filename="live.mp4",
                media_type="video",
            )
            db.add(asset)
            db.commit()
            task = QianchuanDelivery(
                id="retry-live-upload",
                batch_id="batch-live-upload",
                asset_id=asset.id,
                created_by_number="FD-A",
                advertiser_id="1001",
                plan_id="2001",
                plan_type="multiplication",
                upload_task_id="still-processing",
                status="failed",
                failure_stage="upload",
                last_error_category="async_upload_processing",
            )
            db.add(task)
            db.commit()

            result = qianchuan_task_retry(
                task.id, BackgroundTasks(), db, owner
            )

            self.assertEqual(result["status"], "queued")
            current = db.get(QianchuanDelivery, task.id)
            self.assertEqual(current.status, "pending")
            self.assertEqual(current.upload_task_id, "still-processing")
            self.assertIn("续查已有原视频上传任务", current.message)

    def test_regular_user_can_only_manage_own_upload(self):
        owner = {"number": "FD-A", "realName": "甲"}
        colleague = {"number": "FD-B", "realName": "乙"}
        upload = Asset(
            object_key="yxb/uploads/甲/a.mp4",
            filename="a.mp4",
            media_type="video",
            ingest_source="oa_upload",
            uploaded_by_number="FD-A",
        )
        scanned = Asset(object_key="yxb/library/b.mp4", filename="b.mp4", media_type="video")
        self.assertTrue(can_manage_asset(owner, upload))
        self.assertFalse(can_manage_asset(colleague, upload))
        self.assertFalse(can_manage_asset(owner, scanned))

    def test_he_yuting_can_recoverably_delete_but_not_edit_or_purge_foreign_asset(self):
        steward = {"number": "FD-HYT", "realName": "何雨庭", "groupName": "视频中心", "deptJobName": "制作同事"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/library/team-asset.mp4",
                filename="团队素材.mp4",
                media_type="video",
                ingest_source="oss_scan",
                uploaded_by_number="FD-OTHER",
            )
            db.add(asset)
            db.commit()
            self.assertFalse(can_manage_asset(steward, asset))
            self.assertTrue(can_delete_asset(steward, asset))

            deleted = delete_asset(asset.id, db, steward)
            self.assertIsNotNone(deleted.deleted_at)
            self.assertTrue(deleted.can_delete)
            self.assertFalse(deleted.can_purge)
            with self.assertRaises(HTTPException) as blocked:
                permanently_delete_asset(db, db.get(Asset, asset.id), steward)
            self.assertEqual(blocked.exception.status_code, 403)

            restored = restore_asset(asset.id, db, steward)
            self.assertIsNone(restored.deleted_at)
            with self.assertRaises(HTTPException) as blocked_edit:
                update_asset(asset.id, AssetUpdate(filename="不应改名.mp4"), db, steward)
            self.assertEqual(blocked_edit.exception.status_code, 403)

    def test_soft_delete_is_idempotent_and_restore_returns_asset(self):
        now = datetime.utcnow()
        user = {"number": "FD-100", "realName": "测试同事", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/delete/a.mp4",
                filename="a.mp4",
                media_type="video",
                ingest_source="oa_upload",
                uploaded_by_number="FD-100",
            )
            db.add(asset)
            db.commit()
            self.assertTrue(soft_delete_asset(db, asset, user, now))
            self.assertFalse(soft_delete_asset(db, asset, user, now + timedelta(minutes=1)))
            db.commit()
            self.assertEqual(asset.deleted_at, now)
            self.assertEqual(db.query(AssetAuditLog).filter_by(action="trash").count(), 1)

            self.assertTrue(restore_deleted_asset(db, asset, user, now + timedelta(minutes=2)))
            self.assertFalse(restore_deleted_asset(db, asset, user, now + timedelta(minutes=3)))
            db.commit()
            self.assertIsNone(asset.deleted_at)
            self.assertEqual(db.query(AssetAuditLog).filter_by(action="restore").count(), 1)

    def test_delete_trash_restore_flow_keeps_oss_and_active_counts_distinct(self):
        user = {"number": "FD-103", "realName": "流程测试", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/flow/a.mp4",
                filename="a.mp4",
                media_type="video",
                size=99,
                ingest_source="oa_upload",
                uploaded_by_number="FD-103",
            )
            db.add(asset)
            db.commit()
            asset_id = asset.id

            deleted = delete_asset(asset_id, db, user)
            self.assertIsNotNone(deleted.deleted_at)
            page = list_trash("", "deleted", 1, 60, db, user)
            self.assertEqual(page.total, 1)
            counts = stats(db, user)
            self.assertEqual(counts.total, 0)
            self.assertEqual(counts.oss_total, 1)
            self.assertEqual(counts.trash, 1)

            restored = restore_asset(asset_id, db, user)
            self.assertIsNone(restored.deleted_at)
            counts = stats(db, user)
            self.assertEqual(counts.total, 1)
            self.assertEqual(counts.oss_total, 1)
            self.assertEqual(counts.trash, 0)

    def test_batch_tags_and_source_folders_update_only_managed_assets(self):
        owner = {"number": "FD-203", "realName": "素材同事", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            first = Asset(
                object_key="yxb/uploads/fd-203/first.mp4",
                filename="first.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
                ingest_source="oa_upload",
                uploaded_by_number="FD-203",
                tags=["原标签"],
            )
            second = Asset(
                object_key="yxb/uploads/fd-203/second.mp4",
                filename="second.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="source",
                ingest_source="oa_upload",
                uploaded_by_number="FD-203",
                tags=[],
            )
            db.add_all([first, second])
            db.commit()

            tagged = batch_update_asset_tags(
                BatchTagsPayload(asset_ids=[first.id, second.id], tags=["KOC", "林凡清", "KOC"], mode="add"),
                db,
                owner,
            )
            foldered = batch_update_asset_folder(
                BatchFolderPayload(asset_ids=[first.id, second.id], folder_name="KOC/林凡清"),
                db,
                owner,
            )

            self.assertEqual(tagged["count"], 2)
            self.assertEqual(first.tags, ["原标签", "KOC", "林凡清"])
            self.assertEqual(second.tags, ["KOC", "林凡清"])
            self.assertEqual(foldered["folder_name"], "KOC-林凡清")
            self.assertEqual(first.folder_name, "KOC-林凡清")
            self.assertEqual(second.folder_name, "KOC-林凡清")
            self.assertEqual(db.query(AssetAuditLog).filter_by(action="batch_tags").count(), 2)
            self.assertEqual(db.query(AssetAuditLog).filter_by(action="batch_folder").count(), 2)

    def test_batch_folder_rejects_remix_assets(self):
        owner = {"number": "FD-204", "realName": "素材同事", "groupName": "品牌营销部"}
        with SessionLocal() as db:
            remix = Asset(
                object_key="yxb/uploads/fd-204/remix.mp4",
                filename="remix.mp4",
                media_type="video",
                asset_scope="marketing_video",
                library_type="remix",
                ingest_source="oa_upload",
                uploaded_by_number="FD-204",
            )
            db.add(remix)
            db.commit()
            with self.assertRaises(HTTPException) as blocked:
                batch_update_asset_folder(BatchFolderPayload(asset_ids=[remix.id], folder_name="KOC"), db, owner)
            self.assertEqual(blocked.exception.status_code, 422)

    def test_super_admin_can_purge_all_trash_after_exact_confirmation(self):
        admin = {"number": "FD-026222", "realName": "舒豪", "groupName": "品牌营销部"}
        with patch.dict(os.environ, {"SUPER_ADMIN_NUMBERS": "FD-026222"}, clear=False), SessionLocal() as db:
            assets = [
                Asset(
                    object_key=f"yxb/trash/all-{index}.mp4",
                    filename=f"all-{index}.mp4",
                    media_type="video",
                    deleted_at=datetime.utcnow(),
                )
                for index in range(2)
            ]
            db.add_all(assets)
            db.commit()
            with self.assertRaises(HTTPException) as blocked:
                purge_all_trash(TrashClearPayload(confirm_text="确认"), db, admin)
            self.assertEqual(blocked.exception.status_code, 422)

            with patch("app.main.oss_service.delete_asset") as delete_object:
                result = purge_all_trash(TrashClearPayload(confirm_text="永久清空回收站"), db, admin)
            self.assertTrue(result["ok"])
            self.assertEqual(result["checked"], 2)
            self.assertEqual(result["purged"], 2)
            self.assertEqual(delete_object.call_count, 2)
            self.assertTrue(all(asset.purged_at is not None for asset in assets))

    def test_weekly_cleanup_only_purges_items_older_than_retention(self):
        now = datetime.utcnow()
        with SessionLocal() as db:
            expired = Asset(
                object_key="yxb/trash/expired.mp4",
                filename="expired.mp4",
                media_type="video",
                deleted_at=now - timedelta(days=8),
            )
            recent = Asset(
                object_key="yxb/trash/recent.mp4",
                filename="recent.mp4",
                media_type="video",
                deleted_at=now - timedelta(days=2),
            )
            db.add_all([expired, recent])
            db.commit()
            with patch("app.main.oss_service.delete_asset") as delete_object:
                result = purge_deleted_assets(db, now)
            self.assertEqual(result, {"checked": 1, "purged": 1, "failed": 0})
            delete_object.assert_called_once_with("yxb/trash/expired.mp4")
            self.assertEqual(expired.purged_at, now)
            self.assertIsNone(recent.purged_at)

    def test_cleanup_scheduler_runs_once_per_seven_day_window(self):
        now = datetime.utcnow()
        result = {"checked": 0, "purged": 0, "failed": 0}
        with patch("app.main.purge_deleted_assets", return_value=result) as cleanup:
            first = run_trash_cleanup_if_due(now=now)
            waiting = run_trash_cleanup_if_due(now=now + timedelta(days=1))
            second = run_trash_cleanup_if_due(now=now + timedelta(days=7, seconds=1))
        self.assertEqual(first["status"], "completed")
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(cleanup.call_count, 2)

    def test_oss_sync_keeps_trash_and_purged_tombstones(self):
        now = datetime.utcnow()
        with SessionLocal() as db:
            active = Asset(object_key="yxb/missing/active.mp4", filename="active.mp4", media_type="video")
            trashed = Asset(
                object_key="yxb/missing/trashed.mp4",
                filename="trashed.mp4",
                media_type="video",
                deleted_at=now,
            )
            purged = Asset(
                object_key="yxb/missing/purged.mp4",
                filename="purged.mp4",
                media_type="video",
                deleted_at=now - timedelta(days=8),
                purged_at=now,
            )
            db.add_all([active, trashed, purged])
            db.commit()
            with patch("app.main.catalog_service.list_assets", return_value=[]):
                _merge_oss_assets(db, [], now + timedelta(seconds=1))
            self.assertIsNone(db.scalar(db.query(Asset).filter_by(object_key=active.object_key).statement))
            self.assertIsNotNone(db.scalar(db.query(Asset).filter_by(object_key=trashed.object_key).statement))
            self.assertIsNotNone(db.scalar(db.query(Asset).filter_by(object_key=purged.object_key).statement))

    def test_permanent_delete_requires_department_manager(self):
        regular = {"number": "FD-101", "realName": "普通同事", "groupName": "品牌营销部", "deptJobName": "剪辑师"}
        manager = {"number": "FD-102", "realName": "部门主管", "groupName": "品牌营销部", "deptJobName": "营销主管"}
        self.assertFalse(is_asset_admin(regular))
        self.assertTrue(is_asset_admin(manager))
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/trash/manual.mp4",
                filename="manual.mp4",
                media_type="video",
                deleted_at=datetime.utcnow(),
            )
            db.add(asset)
            db.commit()
            with self.assertRaises(Exception) as blocked:
                permanently_delete_asset(db, asset, regular)
            self.assertEqual(getattr(blocked.exception, "status_code", None), 403)
            with patch("app.main.oss_service.delete_asset") as delete_object:
                self.assertTrue(permanently_delete_asset(db, asset, manager))
            delete_object.assert_called_once_with("yxb/trash/manual.mp4")
            self.assertIsNotNone(asset.purged_at)

    def test_oss_delete_rejects_objects_outside_managed_prefix(self):
        service = OssService()
        service.client = MagicMock()
        with self.assertRaises(ValueError):
            service.delete_asset("other/a.mp4")
        service.delete_asset(f"{settings.prefix.rstrip('/')}/a.mp4")
        service.client.delete_object.assert_called_once()

    def test_generated_cover_uses_exact_bytes_for_s3_compatible_hashing(self):
        service = OssService()
        service.client = MagicMock()
        service.client.generate_presigned_url.return_value = "https://oss.example/presigned-cover"
        key = f"{settings.prefix.rstrip('/')}/covers/1-etag.jpg"
        response = MagicMock(status_code=200)
        with patch.object(Path, "read_bytes", return_value=b"cover-bytes"), patch(
            "app.oss_service.requests.put", return_value=response,
        ) as put:
            service.upload_file("cover.jpg", key, "image/jpeg")
        service.client.generate_presigned_url.assert_called_once_with(
            "put_object",
            Params={
                "Bucket": settings.bucket,
                "Key": key,
                "ContentType": "image/jpeg",
                "CacheControl": "public, max-age=31536000, immutable",
            },
            ExpiresIn=600,
        )
        put.assert_called_once_with(
            "https://oss.example/presigned-cover",
            data=b"cover-bytes",
            headers={"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=31536000, immutable"},
            timeout=60,
        )

    def test_qianchuan_status_reports_authorized_without_exposing_tokens(self):
        service = QianchuanService()
        environment = {
            "QIANCHUAN_APP_ID": "123",
            "QIANCHUAN_APP_SECRET": "secret-for-test",
            "QIANCHUAN_ACCESS_TOKEN": "token-for-test",
            "QIANCHUAN_EXPIRES_IN": "86400",
            "QIANCHUAN_TOKEN_SAVED_AT": datetime.now(timezone.utc).isoformat(),
        }
        with patch.dict(os.environ, environment, clear=False):
            with SessionLocal() as db:
                result = service.status(db)
        self.assertTrue(result["authorized"])
        self.assertNotIn("access_token", result)
        self.assertNotIn("app_secret", result)

    def test_qianchuan_connection_error_keeps_a_retryable_diagnostic(self):
        service = QianchuanService()
        with patch(
            "app.qianchuan_service.requests.request",
            side_effect=__import__("requests").ConnectionError("peer reset"),
        ):
            with self.assertRaises(QianchuanError) as failed:
                service._request_json(
                    "POST",
                    "https://api.oceanengine.com/open_api/2/file/video/ad/",
                    token="test-token",
                    files={"video_file": ("a.mp4", b"video")},
                )
        self.assertEqual(failed.exception.category, "connection_interrupted")
        self.assertTrue(failed.exception.retryable)
        self.assertIn("视频传输过程中", str(failed.exception))

    def test_qianchuan_throughput_limit_is_retryable(self):
        service = QianchuanService()
        response = MagicMock(status_code=429)
        response.json.return_value = {
            "code": 4028,
            "message": "Too much throughput in a short period of time, please slow down.",
            "request_id": "rate-limited-request",
        }
        with patch(
            "app.qianchuan_service.requests.request",
            return_value=response,
        ):
            with self.assertRaises(QianchuanError) as failed:
                service._request_json(
                    "POST",
                    "https://api.oceanengine.com/open_api/2/file/video/ad/",
                    token="test-token",
                    data=b"unchanged-video",
                )
        self.assertEqual(failed.exception.category, "rate_limit")
        self.assertTrue(failed.exception.retryable)
        self.assertEqual(str(failed.exception.code), "4028")

    def test_qianchuan_video_upload_uses_original_oss_url_without_multipart_or_transcoding(self):
        service = QianchuanService()
        captured = {}

        def request(_db, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return {"request_id": "request-async", "data": {"task_id": 7788}}

        with patch.object(service, "_object_md5", return_value="a" * 32) as digest, patch.object(
            service, "_video_by_signature", return_value=None
        ) as signature_lookup, patch.object(
            service, "_cached_object_md5", return_value=""
        ), patch("app.qianchuan_service.oss_service.url_for", return_value="https://oss.example/original.mp4"), patch.object(
            service, "_authorized_request", side_effect=request
        ):
            with SessionLocal() as db:
                result = service.begin_video_upload(
                    db,
                    advertiser_id="1001",
                    object_key="yxb/original.mp4",
                    filename="原视频.mp4",
                )

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], ASYNC_VIDEO_UPLOAD_URL)
        self.assertEqual(captured["json_body"]["account_type"], "ADVERTISER")
        self.assertEqual(captured["json_body"]["video_url"], "https://oss.example/original.mp4")
        self.assertNotIn("files", captured)
        self.assertNotIn("data", captured)
        self.assertEqual(result["upload_task_id"], "7788")
        self.assertFalse(result["reused"])
        digest.assert_not_called()
        signature_lookup.assert_not_called()

    def test_qianchuan_async_upload_poll_returns_original_video_id(self):
        service = QianchuanService()
        captured = {}

        def request(_db, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return {
                "request_id": "request-result",
                "data": {
                    "list": [
                        {
                            "task_id": 7788,
                            "status": "SUCCESS",
                            "video_info": {"video_id": "video-original-1"},
                        }
                    ]
                },
            }

        with patch.object(service, "_authorized_request", side_effect=request):
            with SessionLocal() as db:
                result = service.wait_video_upload(
                    db,
                    advertiser_id="1001",
                    upload_task_id="7788",
                )

        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["url"], ASYNC_VIDEO_UPLOAD_RESULT_URL)
        self.assertEqual(captured["params"]["task_ids"], "[7788]")
        self.assertEqual(result["platform_asset_id"], "video-original-1")

    def test_qianchuan_non_tos_url_falls_back_to_unchanged_file_upload(self):
        service = QianchuanService()
        uploaded = {
            "platform_asset_id": "video-original-2",
            "upload_task_id": "",
            "signature": "b" * 32,
            "request_id": "request-direct",
            "reused": False,
        }

        with patch.object(service, "_object_md5", return_value="b" * 32) as digest, patch.object(
            service, "_video_by_signature", return_value=None
        ), patch.object(
            service, "_cached_object_md5", return_value=""
        ), patch.object(
            service, "_save_object_md5"
        ), patch("app.qianchuan_service.oss_service.url_for", return_value="https://oss.example/original.mp4"), patch.object(
            service,
            "_authorized_request",
            side_effect=QianchuanError("URL上传视频仅支持连山云TOS存储"),
        ), patch.object(service, "_upload_original_file_direct", return_value=uploaded) as direct:
            with SessionLocal() as db:
                result = service.begin_video_upload(
                    db,
                    advertiser_id="1001",
                    object_key="yxb/original.mp4",
                    filename="原视频.mp4",
                )

        direct.assert_called_once_with(
            ANY,
            advertiser_id="1001",
            object_key="yxb/original.mp4",
            filename="原视频.mp4",
            signature="",
        )
        self.assertEqual(result["platform_asset_id"], "video-original-2")
        digest.assert_not_called()

    def test_qianchuan_direct_fallback_reads_original_only_once_before_upload(self):
        service = QianchuanService()
        original = b"unchanged-original-video-bytes"
        expected_signature = __import__("hashlib").md5(original).hexdigest()
        request = MagicMock(
            return_value={"request_id": "request-direct-once", "data": {"video_id": "video-direct-once"}}
        )

        with patch.object(type(oss_service), "configured", new_callable=PropertyMock, return_value=True), patch(
            "app.qianchuan_service.oss_service.client.get_object",
            return_value={"Body": BytesIO(original)},
        ) as get_object, patch.object(service, "_save_object_md5") as save_digest, patch.object(
            service, "_authorized_request", request
        ):
            with SessionLocal() as db:
                result = service._upload_original_file_direct(
                    db,
                    advertiser_id="1001",
                    object_key="yxb/original.mp4",
                    filename="原视频.mp4",
                )

        get_object.assert_called_once()
        save_digest.assert_called_once_with(ANY, "yxb/original.mp4", expected_signature)
        self.assertEqual(result["platform_asset_id"], "video-direct-once")
        self.assertEqual(result["signature"], expected_signature)

    def test_qianchuan_authorize_url_preserves_special_business_parameters(self):
        service = QianchuanService()
        environment = {
            "QIANCHUAN_APP_ID": "456",
            "QIANCHUAN_APP_SECRET": "secret-for-test",
            "QIANCHUAN_REDIRECT_URI": "https://example.com/api/qianchuan/oauth/callback",
            "QIANCHUAN_AUTH_URL": (
                "https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html"
                "?app_id=old&state=old&material_auth=true&rid=keep-me"
            ),
        }
        with patch.dict(os.environ, environment, clear=False):
            with SessionLocal() as db:
                authorize_url = service.authorize_url(db)

        parsed = urlsplit(authorize_url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "qianchuan.jinritemai.com")
        self.assertEqual(query["app_id"], ["456"])
        self.assertEqual(query["redirect_uri"], [environment["QIANCHUAN_REDIRECT_URI"]])
        self.assertEqual(query["material_auth"], ["true"])
        self.assertEqual(query["rid"], ["keep-me"])
        self.assertNotEqual(query["state"], ["old"])

    def test_qianchuan_accounts_expand_enterprise_to_all_real_advertisers(self):
        service = QianchuanService()

        def response(_db, _method, url, **_kwargs):
            if url == ACCOUNTS_URL:
                return {
                    "data": {
                        "list": [
                            {
                                "advertiser_id": "9001",
                                "advertiser_name": "企业主体",
                                "role": "PLATFORM_ROLE_ENTERPRISE_BP_OPERATOR",
                            }
                        ]
                    }
                }
            if url == EBP_ADVERTISERS_URL:
                return {
                    "data": {
                        "account_list": [
                            {"advertiser_id": "1002", "advertiser_name": "WIS账户B"},
                            {"advertiser_id": "1001", "advertiser_name": "WIS账户A"},
                        ],
                        "page_info": {"total_page": 1},
                    }
                }
            raise AssertionError(url)

        with patch.object(service, "_authorized_request", side_effect=response):
            with SessionLocal() as db:
                items = service.accounts(db)

        self.assertEqual([item["id"] for item in items], ["1001", "1002"])
        self.assertTrue(all(item["role"] == "QIANCHUAN_ADVERTISER" for item in items))

    def test_qianchuan_unified_plans_explicitly_query_multiplication_and_paginate(self):
        service = QianchuanService()
        requests = []

        def response(_db, _method, _url, **kwargs):
            params = kwargs["params"]
            requests.append(params.copy())
            page = params["page"]
            return {
                "data": {
                    "ad_list": [
                        {
                            "ad_info": {
                                "id": f"20{page}",
                                "name": f"乘方{page}",
                                "status": "ENABLE",
                            }
                        }
                    ],
                    "page_info": {"total_page": 2},
                }
            }

        with patch.object(service, "_plan_api_request", side_effect=response):
            with SessionLocal() as db:
                items = service._unified_plans(db, "1001", "VIDEO_PROM_GOODS", "OVERALL_PROJECT")

        self.assertEqual([item["id"] for item in items], ["201", "202"])
        self.assertTrue(all(item["plan_type"] == "multiplication" for item in items))
        self.assertTrue(all(item["plan_type_label"] == "乘方计划" for item in items))
        self.assertEqual([item["page"] for item in requests], [1, 2])
        self.assertTrue(all(item["adlab_scene"] == "OVERALL_PROJECT" for item in requests))

    def test_qianchuan_plans_load_multiplication_first_and_reuse_source_cache(self):
        service = QianchuanService()
        unified_calls = []

        def unified(_db, _advertiser_id, goal, scene):
            unified_calls.append((goal, scene))
            is_multiplication = scene == "OVERALL_PROJECT"
            return [
                {
                    "id": f"{goal}:{scene}",
                    "name": f"{goal}:{scene}",
                    "status": "ENABLE",
                    "status_label": "投放中",
                    "marketing_goal": goal,
                    "marketing_scene": "",
                    "campaign_scene": scene,
                    "campaign_id": "",
                    "plan_type": "multiplication" if is_multiplication else "full_domain",
                    "plan_type_label": "乘方计划" if is_multiplication else "全域推广",
                    "can_attach_video": True,
                    "direct_add_api": "qianchuan/uni_promotion/ad/material/add",
                }
            ]

        standard = MagicMock(
            side_effect=QianchuanError("missing /qianchuan/ad/get/ permission", code=40002)
        )
        with patch.object(service, "_unified_plans", side_effect=unified), patch.object(
            service, "_standard_plans", standard
        ):
            with SessionLocal() as db:
                multiplication = service.plans(db, "1001", scope="multiplication")
                result = service.plans(db, "1001", scope="all")
                cached = service.plans(db, "1001", scope="multiplication")

        self.assertEqual(multiplication["counts"]["total"], 2)
        self.assertEqual(multiplication["counts"]["multiplication"], 2)
        self.assertFalse(multiplication["cached"])
        self.assertEqual(result["counts"]["total"], 4)
        self.assertEqual(result["counts"]["multiplication"], 2)
        self.assertEqual(result["counts"]["full_domain"], 2)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertTrue(result["cached"])
        self.assertTrue(cached["cached"])
        self.assertEqual(len(unified_calls), 4)
        self.assertEqual(standard.call_count, 1)

    def test_qianchuan_plan_source_keeps_last_success_when_throttled(self):
        service = QianchuanService()
        cache_key = ("1001", "VIDEO_PROM_GOODS", "OVERALL_PROJECT")
        item = {"id": "2001", "name": "乘方计划"}

        with SessionLocal() as db:
            first = service._load_plan_source(db, cache_key, lambda: [item])
            service._plan_source_cache[cache_key]["stored_at"] -= service.plan_cache_seconds + 1
            second = service._load_plan_source(
                db,
                cache_key,
                MagicMock(side_effect=QianchuanError("系统请求频率超限", code=40100)),
            )

        self.assertFalse(first[1])
        self.assertEqual(second[0], [item])
        self.assertTrue(second[1])
        self.assertTrue(second[3])
        self.assertIn("频率超限", second[4])

    def test_qianchuan_plan_source_reuses_persistent_last_success_after_restart(self):
        cache_key = ("1001", "VIDEO_PROM_GOODS", "OVERALL_PROJECT")
        item = {"id": "2001", "name": "乘方计划"}
        first_service = QianchuanService()
        with SessionLocal() as db:
            first_service._load_plan_source(db, cache_key, lambda: [item])
            row = db.get(AppMeta, first_service._plan_cache_meta_key(cache_key))
            payload = json.loads(row.value)
            payload["source_read_at"] = (
                datetime.now(timezone.utc)
                - timedelta(seconds=first_service.plan_cache_seconds + 60)
            ).isoformat()
            row.value = json.dumps(payload, ensure_ascii=False)
            db.commit()

        restarted_service = QianchuanService()
        with SessionLocal() as db:
            result = restarted_service._load_plan_source(
                db,
                cache_key,
                MagicMock(side_effect=QianchuanError("连接千川开放平台中断", retryable=True)),
            )

        self.assertEqual(result[0], [item])
        self.assertTrue(result[1])
        self.assertTrue(result[3])
        self.assertIn("中断", result[4])

    def test_qianchuan_plan_source_cache_only_uses_last_success_without_network(self):
        service = QianchuanService()
        cache_key = ("1002", "VIDEO_PROM_GOODS", "OVERALL_PROJECT")
        item = {"id": "2002", "name": "最近成功计划"}
        loader = MagicMock(return_value=[item])

        with SessionLocal() as db:
            service._load_plan_source(db, cache_key, loader)
            service._plan_source_cache[cache_key]["stored_at"] -= service.plan_stale_seconds + 1
            cached = service._load_plan_source(
                db,
                cache_key,
                MagicMock(side_effect=AssertionError("cache-only must not call network")),
                cache_only=True,
            )

        self.assertEqual(cached[0], [item])
        self.assertTrue(cached[1])
        self.assertTrue(cached[3])
        self.assertIn("最近一次成功", cached[4])

    def test_qianchuan_plan_requests_do_not_hold_rate_lock_while_waiting_for_network(self):
        service = QianchuanService()
        entered = threading.Barrier(2, timeout=2)
        errors = []

        def request(_db, _method, _url, **_kwargs):
            entered.wait()
            return {"data": {"ad_list": [], "page_info": {"total_page": 1}}}

        def run_request():
            try:
                with SessionLocal() as db:
                    service._plan_api_request(db, "GET", "https://example.com/plans")
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch.dict(os.environ, {"QIANCHUAN_PLAN_REQUEST_INTERVAL": "0"}), patch.object(
            service, "_authorized_request", side_effect=request
        ):
            threads = [threading.Thread(target=run_request) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_qianchuan_plan_request_retries_transient_network_error(self):
        service = QianchuanService()
        request = MagicMock(
            side_effect=[
                QianchuanError("连接中断", category="connection_interrupted", retryable=True),
                {"data": {"ad_list": [], "page_info": {"total_page": 1}}},
            ]
        )
        with patch.dict(os.environ, {"QIANCHUAN_PLAN_REQUEST_INTERVAL": "0"}), patch.object(
            service, "_authorized_request", request
        ), patch("app.qianchuan_service.time.sleep"):
            with SessionLocal() as db:
                result = service._plan_api_request(db, "GET", "https://example.com/plans")

        self.assertEqual(result["data"]["page_info"]["total_page"], 1)
        self.assertEqual(request.call_count, 2)

    def test_qianchuan_video_plan_add_uses_product_creative_structure(self):
        service = QianchuanService()
        captured = {}

        def request(_db, _method, _url, **kwargs):
            captured.update(kwargs["json_body"])
            return {"request_id": "request-1", "data": {}}

        detail = {
            "ad_id": 2001,
            "marketing_goal": "VIDEO_PROM_GOODS",
            "multi_product_creative_list": [
                {"product_id": 3001, "aweme_uid": 4001, "video_material": [{"video_id": "a"}, {"video_id": "b"}]},
                {"product_id": 3001, "aweme_uid": 4001, "video_material": [{"video_id": "a"}, {"video_id": "b"}]},
                {"product_id": 3001, "aweme_uid": 4003, "video_material": [{"video_id": "c"}]},
                {"product_id": 3001, "aweme_uid": 4002, "video_material": []},
            ],
        }
        video_material = {
            "video_id": "video-1",
            "image_mode": "VIDEO_VERTICAL",
            "video_cover_id": "cover-1",
        }
        verification = {
            "matched_count": 1,
            "matches": [{"video_id": "video-1"}],
            "request_id": "request-verify-1",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        with patch.object(service, "plan_video_evidence", return_value={"matched_count": 0}), patch.object(
            service, "plan_detail", return_value=detail
        ), patch.object(
            service, "_video_creative_material", return_value=video_material
        ), patch.object(service, "_authorized_request", side_effect=request), patch.object(
            service, "_wait_for_plan_video", return_value=verification
        ):
            with SessionLocal() as db:
                result = service.add_to_plan(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="video-1",
                )

        self.assertEqual(result["marketing_goal"], "VIDEO_PROM_GOODS")
        self.assertNotIn("programmatic_creative_media_list", captured)
        self.assertEqual(
            captured["multi_product_creative_list"],
            [
                {
                    "product_id": 3001,
                    "aweme_uid": 4003,
                    "video_material": [video_material],
                }
            ],
        )

    def test_qianchuan_video_plan_add_bootstraps_primary_product_identity(self):
        service = QianchuanService()
        captured = {}

        def request(_db, _method, _url, **kwargs):
            captured.update(kwargs["json_body"])
            return {"request_id": "request-primary", "data": {}}

        detail = {
            "ad_id": 2001,
            "marketing_goal": "VIDEO_PROM_GOODS",
            "aweme_id": 4001,
            "multi_product_creative_list": [
                {"product_id": 3001, "aweme_uid": 4001, "video_material": []},
                {"product_id": 3001, "aweme_uid": 4999, "video_material": [{"video_id": "collaborator-video"}]},
            ],
        }
        video_material = {
            "video_id": "video-1",
            "image_mode": "VIDEO_VERTICAL",
            "video_cover_id": "cover-1",
        }
        verification = {
            "matched_count": 1,
            "matches": [{"video_id": "video-1"}],
            "request_id": "request-verify-primary",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        with patch.object(service, "plan_video_evidence", return_value={"matched_count": 0}), patch.object(
            service, "plan_detail", return_value=detail
        ), patch.object(
            service, "_video_creative_material", return_value=video_material
        ), patch.object(service, "_authorized_request", side_effect=request), patch.object(
            service, "_wait_for_plan_video", return_value=verification
        ):
            with SessionLocal() as db:
                service.add_to_plan(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="video-1",
                )

        self.assertEqual(
            captured["multi_product_creative_list"],
            [
                {
                    "product_id": 3001,
                    "aweme_uid": 4001,
                    "video_material": [video_material],
                }
            ],
        )

    def test_qianchuan_video_plan_add_does_not_bootstrap_collaborator_identity(self):
        service = QianchuanService()
        detail = {
            "ad_id": 2001,
            "marketing_goal": "VIDEO_PROM_GOODS",
            "aweme_id": 4001,
            "multi_product_creative_list": [
                {"product_id": 3001, "aweme_uid": 4999, "video_material": [{"video_id": "collaborator-video"}]},
            ],
        }
        with patch.object(service, "plan_video_evidence", return_value={"matched_count": 0}), patch.object(
            service, "plan_detail", return_value=detail
        ), patch.object(
            service,
            "_video_creative_material",
            return_value={"video_id": "video-1", "video_cover_id": "cover-1"},
        ), patch.object(service, "_authorized_request") as mutate:
            with SessionLocal() as db:
                with self.assertRaises(QianchuanError) as raised:
                    service.add_to_plan(
                        db,
                        advertiser_id="1001",
                        plan_id="2001",
                        video_id="video-1",
                    )

        self.assertEqual(raised.exception.category, "plan_identity_unavailable")
        self.assertTrue(raised.exception.retryable)
        mutate.assert_not_called()

    def test_qianchuan_live_plan_add_includes_video_ratio_and_cover(self):
        service = QianchuanService()
        captured = {}
        video_material = {
            "video_id": "video-1",
            "image_mode": "VIDEO_VERTICAL",
            "video_cover_id": "cover-1",
        }

        def request(_db, _method, _url, **kwargs):
            captured.update(kwargs["json_body"])
            return {"request_id": "request-2", "data": {}}

        detail = {"ad_id": 2002, "marketing_goal": "LIVE_PROM_GOODS"}
        verification = {
            "matched_count": 1,
            "matches": [{"video_id": "video-1"}],
            "request_id": "request-verify-2",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        with patch.object(service, "plan_video_evidence", return_value={"matched_count": 0}), patch.object(
            service, "plan_detail", return_value=detail
        ), patch.object(
            service, "_video_creative_material", return_value=video_material
        ), patch.object(service, "_authorized_request", side_effect=request), patch.object(
            service, "_wait_for_plan_video", return_value=verification
        ):
            with SessionLocal() as db:
                result = service.add_to_plan(
                    db,
                    advertiser_id="1001",
                    plan_id="2002",
                    video_id="video-1",
                )

        self.assertEqual(result["marketing_goal"], "LIVE_PROM_GOODS")
        self.assertNotIn("multi_product_creative_list", captured)
        self.assertEqual(
            captured["programmatic_creative_media_list"],
            {"video_material": [video_material]},
        )

    def test_qianchuan_video_material_recovers_cover_from_verified_plan(self):
        service = QianchuanService()
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cover/a.mp4", filename="a.mp4", media_type="video")
            db.add(asset)
            db.commit()
            db.add(
                QianchuanDelivery(
                    id="30000000-0000-0000-0000-000000000001",
                    batch_id="batch-cover",
                    asset_id=asset.id,
                    created_by_number="FD-A",
                    advertiser_id="1001",
                    plan_id="2001",
                    platform_asset_id="video-1",
                    binding_verified_at=datetime.utcnow(),
                    status="success",
                )
            )
            db.commit()
            detail = {
                "multi_product_creative_list": [
                    {
                        "video_material": [
                            {
                                "video_id": "video-1",
                                "image_mode": "VIDEO_VERTICAL",
                                "video_cover_id": "cover-existing",
                            }
                        ]
                    }
                ]
            }
            with patch.object(
                service,
                "_authorized_request",
                return_value={"data": {"list": []}},
            ) as request, patch.object(service, "plan_detail", return_value=detail):
                material = service._video_creative_material(
                    db,
                    advertiser_id="1001",
                    video_id="video-1",
                )

            self.assertEqual(material["video_cover_id"], "cover-existing")
            self.assertEqual(request.call_count, 1)
            db.flush()
            self.assertIn("cover-existing", db.get(AppMeta, "qianchuan.video_material.1001.video-1").value)

    def test_qianchuan_plan_verification_checks_programmatic_and_product_video_ids(self):
        service = QianchuanService()
        details = [
            {"ad_id": 2001},
            {
                "ad_id": 2001,
                "programmatic_creative_media_list": {
                    "video_material": [{"video_id": "video-1"}],
                },
                "multi_product_creative_list": [
                    {"video_material": [{"video_id": "video-1"}]},
                ],
            },
        ]
        with patch.object(service, "plan_detail", side_effect=details), patch.object(
            service, "plan_video_evidence"
        ) as fallback, patch(
            "app.qianchuan_service.time.sleep"
        ):
            with SessionLocal() as db:
                matched = service._wait_for_plan_video(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="video-1",
                )
        self.assertEqual(matched["matched_count"], 2)
        fallback.assert_not_called()

    def test_qianchuan_plan_material_readback_is_exact_and_paginated(self):
        service = QianchuanService()
        requests = []

        def response(_db, _method, url, **kwargs):
            self.assertEqual(url, FULL_DOMAIN_MATERIAL_LIST_URL)
            requests.append(kwargs["params"])
            page = kwargs["params"]["page"]
            return {
                "request_id": f"readback-{page}",
                "data": {
                    "ad_material_infos": [
                        {
                            "audit_status": "AUDIT_ACCEPTED",
                            "material_status": "DELIVERY_OK",
                            "product_id_list": [3001],
                            "aweme_id_list": [4001],
                            "material_info": {
                                "video_material": {
                                    "video_id": f"video-{page}",
                                    "material_id": 5000 + page,
                                    "title": f"素材{page}",
                                }
                            },
                        }
                    ],
                    "page_info": {"total_page": 2},
                },
            }

        with patch.object(service, "_plan_api_request", side_effect=response):
            with SessionLocal() as db:
                result = service.plan_material_videos(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                )

        self.assertEqual([item["video_id"] for item in result["videos"]], ["video-1", "video-2"])
        self.assertEqual(result["request_id"], "readback-2")
        self.assertEqual([item["page"] for item in requests], [1, 2])
        self.assertTrue(all('"material_type": "VIDEO"' in item["filtering"] for item in requests))

    def test_qianchuan_retry_does_not_add_when_video_is_already_in_selected_plan(self):
        service = QianchuanService()
        evidence = {
            "advertiser_id": "1001",
            "plan_id": "2001",
            "video_id": "6001",
            "matched_count": 1,
            "matches": [{"video_id": "6001", "material_id": "7001"}],
            "readback_source": "qianchuan/uni_promotion/ad/material/get",
            "request_id": "readback-existing",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        with patch.object(
            service,
            "plan_detail",
            return_value={
                "ad_id": 2001,
                "multi_product_creative_list": [
                    {"video_material": [{"video_id": "6001"}]},
                ],
            },
        ), patch.object(service, "plan_video_evidence", return_value=evidence), patch.object(
            service, "_authorized_request"
        ) as mutate, patch.object(service, "_video_creative_material") as cover:
            with SessionLocal() as db:
                result = service.add_to_plan(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="6001",
                )

        self.assertTrue(result["already_present"])
        self.assertEqual(result["delivery_entity_id"], "2001")
        mutate.assert_not_called()
        cover.assert_not_called()

    def test_qianchuan_deleted_plan_material_requires_reactivation(self):
        service = QianchuanService()
        deleted = {
            "videos": [
                {
                    "video_id": "6001",
                    "material_id": "7001",
                    "material_status": "DELETED",
                    "delivery_ready": False,
                    "needs_reactivation": True,
                }
            ],
            "request_id": "readback-deleted",
        }
        with patch.object(service, "plan_material_videos", return_value=deleted), patch.object(
            service, "plan_detail"
        ) as detail:
            with SessionLocal() as db:
                evidence = service.plan_video_evidence(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="6001",
                )

        self.assertEqual(evidence["found_count"], 1)
        self.assertEqual(evidence["matched_count"], 0)
        self.assertTrue(evidence["needs_reactivation"])
        detail.assert_not_called()

    def test_qianchuan_material_metrics_uses_official_fields_and_links(self):
        service = QianchuanService()
        captured = {}

        def response(_db, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return {
                "request_id": "metrics-1",
                "data": {
                    "list": [
                        {
                            "material_id": 6001,
                            "fields": {
                                "stat_cost": 123.45,
                                "pay_order_amount": 456.78,
                                "prepay_and_pay_order_roi": 3.7,
                            },
                            "related_ad_ids": [2001],
                            "related_creative_ids": [3001, 3002],
                        }
                    ],
                    "page_info": {"total_page": 1},
                },
            }

        with patch.object(service, "_authorized_request", side_effect=response):
            with SessionLocal() as db:
                result = service.material_metrics(
                    db,
                    advertiser_id="1001",
                    material_id="6001",
                    start_date="2026-08-01",
                    end_date="2026-08-07",
                )

        self.assertEqual(captured["url"], MATERIAL_REPORT_URL)
        self.assertIn('"material_type": "VIDEO"', captured["params"]["filtering"])
        self.assertEqual(result["metrics"]["stat_cost"], 123.45)
        self.assertEqual(result["metrics"]["prepay_and_pay_order_roi"], 3.7)
        self.assertEqual(result["related_ad_ids"], ["2001"])
        self.assertEqual(result["related_creative_ids"], ["3001", "3002"])

    def test_qianchuan_plan_material_metrics_are_scoped_to_plan_and_video(self):
        service = QianchuanService()
        captured = {}
        material_rows = {
            "request_id": "plan-metrics-1",
            "videos": [
                {
                    "video_id": "6001",
                    "stats_info": {
                        "stat_cost_for_roi2": 88.5,
                        "total_pay_order_gmv_include_coupon_for_roi2": 265.5,
                        "total_pay_order_count_for_roi2": 3,
                        "total_prepay_and_pay_order_roi2": 3.0,
                        "product_show_count_for_roi2": 1000,
                    },
                },
                {"video_id": "other-video", "stats_info": {"stat_cost_for_roi2": 999}},
            ],
        }

        def read(_db, **kwargs):
            captured.update(kwargs)
            return material_rows

        with patch.object(service, "plan_material_videos", side_effect=read):
            with SessionLocal() as db:
                result = service.plan_material_metrics(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="6001",
                    start_date="2026-08-01",
                    end_date="2026-08-07",
                )

        self.assertEqual(captured["plan_id"], "2001")
        self.assertEqual(captured["start_date"], "2026-08-01")
        self.assertEqual(result["metrics"]["stat_cost"], 88.5)
        self.assertEqual(result["metrics"]["pay_order_amount"], 265.5)
        self.assertEqual(result["metrics"]["prepay_and_pay_order_roi"], 3.0)
        self.assertEqual(result["related_ad_ids"], ["2001"])
        self.assertTrue(result["link_verified"])

    def test_qianchuan_deleted_plan_material_is_not_reported_as_linked_data(self):
        service = QianchuanService()
        rows = {
            "request_id": "plan-metrics-deleted",
            "videos": [
                {
                    "video_id": "6001",
                    "material_status": "DELETED",
                    "needs_reactivation": True,
                    "stats_info": {"stat_cost_for_roi2": 0},
                }
            ],
        }
        with patch.object(service, "plan_material_videos", return_value=rows):
            with SessionLocal() as db:
                result = service.plan_material_metrics(
                    db,
                    advertiser_id="1001",
                    plan_id="2001",
                    video_id="6001",
                    start_date="2026-08-01",
                    end_date="2026-08-07",
                )

        self.assertFalse(result["link_verified"])
        self.assertFalse(result["has_data"])
        self.assertEqual(result["found_count"], 1)
        self.assertEqual(result["related_ad_ids"], [])

    def test_qianchuan_metrics_readback_downgrades_a_removed_plan_video(self):
        result = {
            "metrics": {"metric_scope": "uni_promotion_plan_material_roi2"},
            "related_ad_ids": [],
            "related_creative_ids": [],
            "start_date": "2026-08-01",
            "end_date": "2026-08-07",
            "has_data": False,
            "link_verified": False,
            "found_count": 1,
            "matches": [],
            "source": "qianchuan/uni_promotion/ad/material/get",
            "request_id": "readback-missing",
        }
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/removed/a.mp4", filename="a.mp4", media_type="video")
            db.add(asset)
            db.commit()
            task = QianchuanDelivery(
                id="20000000-0000-0000-0000-000000000001",
                batch_id="batch-removed",
                asset_id=asset.id,
                created_by_number="FD-A",
                advertiser_id="1001",
                plan_id="2001",
                plan_type="multiplication",
                platform_asset_id="video-1",
                delivery_entity_id="2001",
                binding_verified_at=datetime.utcnow(),
                status="success",
            )
            db.add(task)
            db.commit()
            with patch("app.main.qianchuan_service.plan_material_metrics", return_value=result):
                _sync_qianchuan_task_metrics(db, task, None, None)

            self.assertEqual(task.status, "partial")
            self.assertEqual(task.metrics_link_status, "missing")
            self.assertEqual(task.failure_stage, "plan_binding")
            self.assertEqual(task.last_error_category, "plan_material_removed")
            self.assertIn("DELETED", task.error_message)

    def test_adq_status_is_authorized_without_exposing_tokens(self):
        service = AdqService()
        environment = {
            "ADQ_APP_ID": "1112041341",
            "ADQ_APP_SECRET": "secret-for-test",
            "ADQ_ACCESS_TOKEN": "access-for-test",
            "ADQ_REFRESH_TOKEN": "refresh-for-test",
            "ADQ_TOKEN_APP_ID": "1112041341",
            "ADQ_ACCOUNT_ID": "65909064",
        }
        with patch.dict(os.environ, environment, clear=False), SessionLocal() as db:
            result = service.status(db)
        self.assertTrue(result["authorized"])
        self.assertNotIn("access_token", result)
        self.assertNotIn("refresh_token", result)
        self.assertNotIn("app_secret", result)

    def test_adq_user_authorization_callback_stores_token_without_exposing_it(self):
        service = AdqService()
        with patch.dict(os.environ, {"ADQ_APP_ID": "1112041797"}, clear=False), SessionLocal() as db:
            url = service.user_authorize_url(
                db,
                redirect_uri="https://app.example.com/api/adq/user-authorization/callback",
                owner_number="FD-A",
            )
            state = parse_qs(urlsplit(url).query)["state"][0]
            result = service.complete_user_authorization(
                db,
                state=state,
                user_status=2,
                user_token="real-name-token-for-test",
                expire_time=str(int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())),
            )
            self.assertEqual(db.get(AppMeta, "adq.user_token_app_id").value, "1112041797")
        self.assertTrue(result["authorized"])
        self.assertNotIn("user_token", result)
        self.assertNotIn("real-name-token-for-test", json.dumps(result))

    def test_adq_restricted_write_places_user_token_in_query_not_json_body(self):
        service = AdqService()
        environment = {
            "ADQ_APP_ID": "1112041797",
            "ADQ_ACCOUNT_ID": "65909064",
            "ADQ_ACCESS_TOKEN": "access-token-for-test",
            "ADQ_TOKEN_APP_ID": "1112041797",
        }
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 0, "data": {"id": "ok"}}
        with patch.dict(os.environ, environment, clear=False), SessionLocal() as db:
            db.add_all([
                AppMeta(key="adq.user_token", value="real-name-token-for-test"),
                AppMeta(key="adq.user_token_app_id", value="1112041797"),
                AppMeta(
                    key="adq.user_token_expires_at",
                    value=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                ),
            ])
            db.commit()
            with patch("app.adq_service.requests.request", return_value=response) as request:
                service._request(
                    db,
                    "POST",
                    "https://api.e.qq.com/v3.0/dynamic_creatives/add",
                    json_body={"account_id": 65909064},
                    requires_user_token=True,
                )
        call = request.call_args
        self.assertEqual(call.kwargs["params"]["user_token"], "real-name-token-for-test")
        self.assertNotIn("user_token", call.kwargs["json"])

    def test_adq_supports_existing_client_id_environment_names(self):
        service = AdqService()
        environment = {
            "ADQ_APP_ID": "",
            "ADQ_APP_SECRET": "",
            "ADQ_CLIENT_ID": "1112041341",
            "ADQ_CLIENT_SECRET": "existing-secret",
            "ADQ_ACCESS_TOKEN": "access-for-test",
            "ADQ_TOKEN_APP_ID": "1112041341",
            "ADQ_ACCOUNT_ID": "65909064",
        }
        with patch.dict(os.environ, environment, clear=False), SessionLocal() as db:
            result = service.status(db)
        self.assertTrue(result["configured"])
        self.assertTrue(result["authorized"])

    def test_adq_refresh_reuses_token_rotated_by_another_request(self):
        service = AdqService()
        with SessionLocal() as db:
            db.add_all([
                AppMeta(key="adq.app_id", value="1112041341"),
                AppMeta(key="adq.access_token", value="new-access-token"),
                AppMeta(key="adq.refresh_token", value="new-refresh-token"),
            ])
            db.commit()
            with patch.dict(os.environ, {"ADQ_APP_ID": "1112041341"}, clear=False), patch(
                "app.adq_service.requests.get"
            ) as request:
                token = service.refresh_access_token(db, stale_access_token="old-access-token")
        self.assertEqual(token, "new-access-token")
        request.assert_not_called()

    def test_adq_never_reuses_tokens_from_another_app(self):
        service = AdqService()
        with SessionLocal() as db:
            db.add_all([
                AppMeta(key="adq.app_id", value="1112041341"),
                AppMeta(key="adq.access_token", value="old-app-access-token"),
                AppMeta(key="adq.refresh_token", value="old-app-refresh-token"),
            ])
            db.commit()
            with patch.dict(
                os.environ,
                {
                    "ADQ_APP_ID": "1112041797",
                    "ADQ_APP_SECRET": "new-app-secret",
                    "ADQ_ACCOUNT_ID": "68377257",
                    "ADQ_ACCESS_TOKEN": "old-env-access-token",
                    "ADQ_REFRESH_TOKEN": "old-env-refresh-token",
                    "ADQ_TOKEN_APP_ID": "1112041341",
                },
                clear=False,
            ):
                result = service.status(db)
                self.assertEqual(service._access_token(db), "")
                self.assertEqual(service._refresh_token(db), "")
        self.assertFalse(result["authorized"])
        self.assertFalse(result["authorization_matches_app"])
        self.assertIn("旧应用令牌已隔离", result["message"])

    def test_adq_stored_tokens_are_bound_to_current_app(self):
        service = AdqService()
        with patch.dict(os.environ, {"ADQ_APP_ID": "1112041797"}, clear=False), SessionLocal() as db:
            service._store_tokens(
                db,
                {
                    "access_token": "new-app-access-token",
                    "refresh_token": "new-app-refresh-token",
                    "expires_in": 3600,
                },
            )
            self.assertEqual(db.get(AppMeta, "adq.app_id").value, "1112041797")
            self.assertEqual(service._access_token(db), "new-app-access-token")

    def test_adq_account_name_uses_corporation_not_plan_alias(self):
        service = AdqService()
        with patch.dict(os.environ, {"ADQ_ACCOUNT_ID": "65909064"}, clear=False), patch.object(
            service,
            "_request",
            return_value={
                "code": 0,
                "data": {
                    "list": [{
                        "account_id": 65909064,
                        "corporation_name": "广州慕可生物科技有限公司",
                        "business_alias": "ZXL投放直充",
                    }]
                },
            },
        ):
            with SessionLocal() as db:
                result = service.account(db)
        self.assertEqual(result["account_name"], "广州慕可生物科技有限公司")
        self.assertNotEqual(result["account_name"], "ZXL投放直充")

    def test_adq_accounts_supports_explicit_delivery_allowlist(self):
        service = AdqService()
        environment = {
            "ADQ_ACCOUNT_ID": "80431518",
            "ADQ_ACCOUNT_IDS": "65909064, 68377257;80431518",
            "ADQ_CATALOG_ACCOUNT_IDS": "80431518,65909064,68377257",
        }
        discovered = [
            {"account_id": "65909064", "account_name": "投放一", "corporation_name": "投放一", "system_status": ""},
            {"account_id": "68377257", "account_name": "投放二", "corporation_name": "投放二", "system_status": ""},
        ]
        with patch.dict(os.environ, environment, clear=False), patch.object(
            service,
            "_business_manager_accounts",
            return_value=discovered,
        ), patch.object(
            service,
            "_request",
            return_value={"code": 0, "data": {"list": [{"account_id": 80431518, "corporation_name": "素材源"}]}},
        ):
            with SessionLocal() as db:
                result = service.accounts(db)
            configured_account_ids = service.configured_account_ids
        self.assertEqual(configured_account_ids, ["80431518", "65909064", "68377257"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["discovered_total"], 2)
        self.assertTrue(result["complete"])

    def test_adq_accounts_fall_back_to_delivery_token_for_known_business_unit_accounts(self):
        service = AdqService()
        environment = {
            "ADQ_ACCOUNT_ID": "",
            "ADQ_ACCOUNT_IDS": "",
            "ADQ_CATALOG_ACCOUNT_IDS": "65909064",
        }

        def detail(_db, account_id, *, token_profile="delivery", fresh=False):
            self.assertEqual(account_id, "65909064")
            if token_profile == "catalog":
                raise AdqError("主体目录令牌不可用", category="authorization")
            return {"account_id": account_id, "account_name": "WIS 投放账户", "corporation_name": "WIS", "system_status": ""}

        with patch.dict(os.environ, environment, clear=False), patch.object(
            service,
            "_business_manager_accounts",
            side_effect=AdqError("商务管家目录未返回", category="account_unavailable"),
        ), patch.object(service, "_account_detail", side_effect=detail):
            with SessionLocal() as db:
                result = service.accounts(db)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["account_id"], "65909064")
        with SessionLocal() as db:
            self.assertEqual(service._account_token_profile(db, "65909064"), "delivery")

    def test_adq_business_manager_accounts_use_complete_legacy_pagination(self):
        service = AdqService()
        with patch.object(
            service,
            "_fetch_all",
            return_value=[
                {"account_id": 65909064, "corporation_name": "广州慕可生物科技有限公司"},
                {"account_id": 65909064, "corporation_name": "重复记录"},
                {"account_id": 68377257, "corporation_name": "投放账户二"},
            ],
        ) as fetch_all, SessionLocal() as db:
            result = service._business_manager_accounts(db, fresh=True)
        self.assertEqual([item["account_id"] for item in result], ["65909064", "68377257"])
        self.assertEqual(fetch_all.call_args.args[1], "https://api.e.qq.com/v1.3/business_manager_relations/get")
        self.assertEqual(fetch_all.call_args.kwargs["page_size"], 100)
        self.assertFalse(fetch_all.call_args.kwargs["pagination_mode"])

    def test_adq_business_unit_ids_are_grouping_metadata_not_advertiser_ids(self):
        service = AdqService()
        environment = {
            "ADQ_BUSINESS_UNIT_IDS": "67858196,85526701",
            "ADQ_BUSINESS_UNIT_67858196_ACCOUNT_IDS": "80431518,65909064",
            "ADQ_BUSINESS_UNIT_85526701_ACCOUNT_IDS": "",
        }
        with patch.dict(os.environ, environment, clear=False):
            units = service.business_units_config
            catalog_accounts = service.configured_catalog_account_ids
        self.assertEqual([item["business_unit_id"] for item in units], ["67858196", "85526701"])
        self.assertEqual(units[0]["account_ids"], ["80431518", "65909064"])
        self.assertEqual(units[1]["account_ids"], [])
        self.assertNotIn("85526701", catalog_accounts)

    def test_adq_catalog_token_isolated_from_accepted_delivery_token(self):
        service = AdqService()
        environment = {
            "ADQ_APP_ID": "1112041797",
            "ADQ_TOKEN_APP_ID": "1112041797",
            "ADQ_ACCESS_TOKEN": "accepted-upload-token",
            "ADQ_CATALOG_TOKEN_APP_ID": "1112041797",
            "ADQ_CATALOG_ACCESS_TOKEN": "subject-catalog-token",
        }
        with patch.dict(os.environ, environment, clear=False), SessionLocal() as db:
            self.assertEqual(service.token(db), "accepted-upload-token")
            self.assertEqual(service.catalog_token(db), "subject-catalog-token")

    def test_adq_hierarchy_keeps_unread_counts_unavailable_instead_of_zero(self):
        service = AdqService()
        environment = {
            "ADQ_APP_ID": "1112041797",
            "ADQ_CATALOG_ACCESS_TOKEN": "",
            "ADQ_CATALOG_REFRESH_TOKEN": "",
            "ADQ_BUSINESS_UNIT_IDS": "67858196,85526701",
            "ADQ_BUSINESS_UNIT_67858196_ACCOUNT_IDS": "",
            "ADQ_BUSINESS_UNIT_85526701_ACCOUNT_IDS": "",
        }
        with patch.dict(os.environ, environment, clear=False), SessionLocal() as db:
            result = service.business_hierarchy(db)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["active_account_total"])
        self.assertIsNone(result["campaign_total"])
        self.assertIn("不按 0 处理", result["message"])

    def test_adq_hierarchy_counts_active_accounts_and_non_deleted_campaigns(self):
        service = AdqService()
        environment = {
            "ADQ_APP_ID": "1112041797",
            "ADQ_CATALOG_TOKEN_APP_ID": "1112041797",
            "ADQ_CATALOG_ACCESS_TOKEN": "subject-catalog-token",
            "ADQ_BUSINESS_UNIT_IDS": "67858196,85526701",
            "ADQ_BUSINESS_UNIT_67858196_ACCOUNT_IDS": "80431518",
            "ADQ_BUSINESS_UNIT_85526701_ACCOUNT_IDS": "65909064",
            "ADQ_EXPECTED_ACTIVE_ACCOUNT_TOTAL": "2",
            "ADQ_EXPECTED_CAMPAIGN_TOTAL": "3",
        }

        def account_detail(_db, account_id, *, token_profile="delivery"):
            self.assertEqual(token_profile, "catalog")
            return {
                "account_id": account_id,
                "account_name": f"账户 {account_id}",
                "corporation_name": "广州慕可生物科技有限公司",
                "system_status": "CUSTOMER_STATUS_NORMAL",
            }

        campaigns_by_account = {
            "80431518": [
                {"campaign_id": "1", "campaign_name": "计划一", "configured_status": "AD_STATUS_NORMAL"},
                {"campaign_id": "2", "campaign_name": "计划二", "configured_status": "AD_STATUS_SUSPEND"},
            ],
            "65909064": [
                {"campaign_id": "3", "campaign_name": "计划三", "configured_status": "AD_STATUS_NORMAL"},
            ],
        }
        with patch.dict(os.environ, environment, clear=False), patch.object(
            service, "_related_business_account_ids", side_effect=lambda _db, seed_account_id, fresh=False: [seed_account_id]
        ), patch.object(service, "_account_detail", side_effect=account_detail), patch.object(
            service, "campaigns", side_effect=lambda _db, account_id, fresh=False: campaigns_by_account[account_id]
        ), SessionLocal() as db:
            result = service.business_hierarchy(db, fresh=True)
        self.assertTrue(result["complete"])
        self.assertTrue(result["count_matches_reference"])
        self.assertEqual(result["active_account_total"], 2)
        self.assertEqual(result["campaign_total"], 3)

    def test_adq_uses_500mb_original_video_limit_without_transcoding(self):
        service = AdqService()
        self.assertEqual(service.shared_library_config()["max_video_mb"], 500)
        with SessionLocal() as db, self.assertRaises(AdqError) as raised:
            service.upload_original_video(
                db,
                account_id="65909064",
                object_key="yxb/adq/too-large.mp4",
                filename="too-large.mp4",
                size=501 * 1024 * 1024,
            )
        self.assertEqual(raised.exception.category, "video_too_large")
        self.assertIn("500MB", str(raised.exception))

    def test_adq_rejects_account_outside_delivery_allowlist(self):
        service = AdqService()
        with patch.dict(
            os.environ,
            {"ADQ_ACCOUNT_ID": "80431518", "ADQ_ACCOUNT_IDS": "65909064"},
            clear=False,
        ), patch.object(service, "_business_manager_accounts", return_value=[]), SessionLocal() as db:
            with self.assertRaises(AdqError) as raised:
                service.account(db, account_id="99999999")
        self.assertEqual(raised.exception.category, "account_unavailable")

    def test_adq_token_errors_are_actionable_and_localized(self):
        message, retryable = AdqService._friendly_platform_message(
            "The RefreshToken is being used. Please try again later."
        )
        self.assertIn("正在刷新授权", message)
        self.assertTrue(retryable)
        message, retryable = AdqService._friendly_platform_message("RefreshToken不存在")
        self.assertIn("授权已失效", message)
        self.assertFalse(retryable)

    def test_adq_adgroups_are_complete_and_only_video_templates_are_attachable(self):
        service = AdqService()
        groups = [
            {"adgroup_id": 1, "adgroup_name": "ZXL投放直充", "system_status": "AD_STATUS_NORMAL"},
            {"adgroup_id": 2, "adgroup_name": "仅视频号", "system_status": "AD_STATUS_NORMAL"},
        ]
        creatives = [
            {
                "dynamic_creative_id": 10,
                "adgroup_id": 1,
                "creative_template_id": 88,
                "creative_components": {"video": [{"component_id": 100}]},
            },
            {
                "dynamic_creative_id": 11,
                "adgroup_id": 2,
                "creative_components": {"video_channels_content": [{"component_id": 101}]},
            },
        ]
        with patch.dict(os.environ, {"ADQ_ACCOUNT_ID": "65909064"}, clear=False), patch.object(
            service, "_fetch_all", side_effect=[groups, creatives]
        ):
            with SessionLocal() as db:
                result = service.adgroups(db, account_id="65909064", fresh=True)
        self.assertEqual(result["source_total"], 2)
        self.assertEqual(result["attachable_total"], 1)
        self.assertTrue(result["items"][0]["can_attach_video"])
        self.assertFalse(result["items"][1]["can_attach_video"])

    def test_adq_creative_payload_reuses_non_video_components_and_replaces_video(self):
        source = {
            "creative_components": {
                "description": [{"component_id": 1, "value": {"description": "旧文案"}}],
                "brand": [{"component_id": 2}],
                "video": [{"component_id": 3, "value": {"video_id": 100}}],
                "video_channels_content": [{"component_id": 4}],
            }
        }
        result = AdqService._component_refs_with_video(source, "200", "300")
        self.assertEqual(result["description"], [{"component_id": 1}])
        self.assertEqual(result["brand"], [{"component_id": 2}])
        self.assertEqual(result["video"], [{"value": {"video_id": "200", "cover_id": "300"}}])
        self.assertNotIn("video_channels_content", result)

    def test_adq_known_account_falls_back_to_delivery_token_without_catalog_authorization(self):
        service = AdqService()
        with patch.object(service, "_catalog_access_token", return_value=""), patch.object(
            service, "_catalog_refresh_token", return_value=""
        ):
            with SessionLocal() as db:
                self.assertEqual(service._account_token_profile(db, "78391277"), "delivery")

    def test_adq_metrics_distinguishes_no_data_from_real_zero(self):
        service = AdqService()
        with patch.object(service, "_fetch_all", return_value=[]):
            with SessionLocal() as db:
                missing = service.material_metrics(
                    db,
                    account_id="65909064",
                    adgroup_id="80291017671",
                    video_id="32012509718",
                    start_date="2026-08-01",
                    end_date="2026-08-01",
                )
        with patch.object(service, "_fetch_all", return_value=[{
            "date": "2026-08-01", "adgroup_id": 80291017671, "video_id": 32012509718,
            "cost": 0, "impression": 0, "click": 0,
        }]):
            with SessionLocal() as db:
                zero = service.material_metrics(
                    db,
                    account_id="65909064",
                    adgroup_id="80291017671",
                    video_id="32012509718",
                    start_date="2026-08-01",
                    end_date="2026-08-01",
                )
        self.assertFalse(missing["has_data"])
        self.assertEqual(missing["metrics"], {})
        self.assertTrue(zero["has_data"])
        self.assertEqual(zero["metrics"]["cost_yuan"], 0)

    def test_adq_root_metrics_persists_verified_material_id(self):
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/adq/root-material-id.mp4",
                filename="root-material-id.mp4",
                media_type="video",
            )
            db.add(asset)
            db.flush()
            task = AdqDelivery(
                id="adq-root-material-id",
                batch_id="adq-root-material-id",
                asset_id=asset.id,
                created_by_number="FD-A",
                account_id="78391277",
                adgroup_id="117461221009",
                platform_asset_id="46782586355",
                status="success",
            )
            db.add(task)
            _apply_adq_metrics_result(db, task, {
                "metrics": {"cost_yuan": 1.0},
                "daily": [],
                "has_data": True,
                "material_id": "46782586355",
                "material_id_status": "verified",
                "start_date": "2026-08-01",
                "end_date": "2026-08-20",
                "message": "根数据已确认素材 ID",
            })
            db.commit()
            db.refresh(task)

            self.assertEqual(task.root_material_id, "46782586355")
            self.assertEqual(task.metrics_status, "success")

    def test_adq_account_material_metrics_uses_new_fields_and_preserves_missing_values(self):
        service = AdqService()
        rows = [
            {
                "date": "2026-06-18",
                "video_id": "26381963823",
                "cost": 16019,
                "view_count": 4893,
                "valid_click_count": 100,
                "order_amount": 35500,
                "order_roi": 2.216081,
                "order_net_amount": 26997,
                "order_net_roi": 1.685283,
            },
            {
                "date": "2026-06-18",
                "video_id": "other-video",
                "cost": 0,
                "view_count": 0,
                "valid_click_count": 0,
            },
        ]
        with patch.dict(os.environ, {"ADQ_ACCOUNT_ID": "80431518"}, clear=False), patch.object(
            service,
            "_material_report_rows",
            return_value=rows,
        ):
            with SessionLocal() as db:
                result = service.account_material_metrics(
                    db,
                    account_id="80431518",
                    start_date="2026-06-18",
                    end_date="2026-06-18",
                )
        self.assertTrue(result["has_data"])
        self.assertEqual(result["video_count"], 2)
        self.assertEqual(result["metrics"]["cost_yuan"], 160.19)
        self.assertEqual(result["metrics"]["order_amount_yuan"], 355.0)
        self.assertEqual(result["metrics"]["order_net_amount_yuan"], 269.97)
        self.assertNotIn("order_24h_by_click_amount_yuan", result["metrics"])

    def test_adq_shared_authorization_uses_company_mdm_scope(self):
        service = AdqService()
        with patch.object(service, "_request", return_value={"code": 0, "data": {"fail_reason": []}}) as request:
            with SessionLocal() as db:
                result = service.grant_all_videos_to_mdm(
                    db,
                    source_account_id="80431518",
                    mdm_id="33471608",
                )
        self.assertEqual(result["scope"], "all_current_and_future_accounts")
        self.assertEqual(request.call_args.args[1:3], ("POST", "https://api.e.qq.com/v1.1/asset_permissions/add"))
        body = request.call_args.kwargs["data"]
        self.assertEqual(body["account_id"], "80431518")
        self.assertEqual(body["licensing_id_type"], "ASSET_TYPE_GROUP_MDM")
        self.assertEqual(body["path_id"], "33471608")
        self.assertIn("ASSET_PERMISSION_GRANT_TYPE_ACCOUNT", body["asset_permission_spec"])
        self.assertNotIn("headers", request.call_args.kwargs)

    def test_adq_original_upload_uses_legacy_upload_contract_without_transcoding(self):
        service = AdqService()
        fake_client = MagicMock()
        fake_client.get_object.return_value = {"Body": BytesIO(b"test")}
        with patch.object(oss_service, "client", fake_client), patch.object(
            service,
            "_find_video_by_signature",
            return_value=None,
        ), patch.object(
            service,
            "_video_detail",
            return_value={"video_id": "video-100", "cover_id": "cover-100"},
        ), patch.object(
            service,
            "_request",
            return_value={"code": 0, "data": {"video_id": "video-100"}},
        ) as request:
            with SessionLocal() as db:
                result = service.upload_original_video(
                    db,
                    account_id="80431518",
                    object_key="yxb/uploads/test.mp4",
                    filename="测试原视频.mp4",
                    size=4,
                )
        self.assertEqual(result["video_id"], "video-100")
        self.assertEqual(request.call_args.args[1:3], ("POST", "https://api.e.qq.com/v1.3/videos/add"))
        encoder = request.call_args.kwargs["data"]
        self.assertIn("video_file", encoder.fields)
        self.assertEqual(encoder.fields["description"], "测试原视频")

    def test_adq_video_detail_uses_current_media_id_filter(self):
        service = AdqService()
        with patch.object(
            service,
            "_request",
            return_value={"code": 0, "data": {"list": [{"video_id": "video-100"}]}},
        ) as request:
            with SessionLocal() as db:
                result = service._video_detail(db, account_id="80431518", video_id="video-100")
        self.assertEqual(result["video_id"], "video-100")
        filtering = json.loads(request.call_args.kwargs["params"]["filtering"])
        self.assertEqual(filtering[0]["field"], "media_id")

    def test_adq_shared_upload_creates_private_original_video_tasks(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            assets = [
                Asset(object_key=f"yxb/adq/shared-{name}.mp4", filename=f"shared-{name}.mp4", media_type="video", size=12)
                for name in ("a", "b")
            ]
            db.add_all(assets)
            db.commit()
            with patch.object(adq_service, "status", return_value={"authorized": True, "message": "授权可用"}):
                result = adq_shared_library_upload(
                    AdqSharedUploadCreate(asset_ids=[asset.id for asset in assets]),
                    db,
                    owner,
                )
            saved = db.scalars(select(AdqDelivery).where(AdqDelivery.batch_id == result["batch_id"])).all()
        self.assertEqual(result["new_task_count"], 2)
        self.assertEqual({task.created_by_number for task in saved}, {"FD-A"})
        self.assertEqual({task.account_id for task in saved}, {"80431518"})
        self.assertEqual({task.adgroup_id for task in saved}, {"__shared_library__"})
        self.assertTrue(all(task.metrics_status == "pending" for task in saved))

    def test_adq_shared_worker_uploads_original_then_grants_without_creating_creative(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/adq/shared-worker.mp4", filename="shared-worker.mp4", media_type="video", size=12)
            db.add(asset)
            db.commit()
            task = AdqDelivery(
                id="adq-shared-worker",
                batch_id="adq-shared-batch",
                asset_id=asset.id,
                created_by_number="FD-A",
                account_id="80431518",
                account_name="ADQ 统一素材源账户 80431518",
                adgroup_id="__shared_library__",
                adgroup_name="公司主体全账户共享素材库",
                status="pending",
            )
            db.add(task)
            db.commit()
        with patch.object(
            adq_service,
            "upload_original_video",
            return_value={"video_id": "video-100", "cover_id": "cover-100", "request_id": "upload-request"},
        ), patch.object(
            adq_service,
            "verify_video_in_library",
            return_value={"verified": True, "account_id": "80431518", "video_id": "video-100", "cover_id": "cover-100", "checked_at": "2026-08-20T10:00:00Z"},
        ), patch.object(
            adq_service,
            "grant_all_videos_to_mdm",
            return_value={"source_account_id": "80431518", "mdm_id": "33471608", "scope": "all_current_and_future_accounts", "failures": [], "request_id": "grant-request"},
        ), patch.object(adq_service, "add_to_adgroup") as bind:
            run_adq_push("adq-shared-worker")
        with SessionLocal() as db:
            saved = db.get(AdqDelivery, "adq-shared-worker")
            self.assertEqual(saved.status, "success")
            self.assertEqual(saved.platform_asset_id, "video-100")
            self.assertEqual(saved.metrics_status, "pending")
            self.assertTrue(saved.binding_evidence["verified"])
            self.assertTrue(saved.binding_evidence["library_readback"]["verified"])
        bind.assert_not_called()

    def test_adq_shared_worker_does_not_claim_success_before_exact_library_readback(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/adq/readback-pending.mp4", filename="readback-pending.mp4", media_type="video", size=12)
            db.add(asset)
            db.commit()
            db.add(AdqDelivery(
                id="adq-readback-pending",
                batch_id="adq-readback-batch",
                asset_id=asset.id,
                created_by_number="FD-A",
                account_id="80431518",
                account_name="ADQ 统一素材源账户 80431518",
                adgroup_id="__shared_library__",
                adgroup_name="公司主体全账户共享素材库",
                status="pending",
            ))
            db.commit()
        with patch.object(
            adq_service,
            "upload_original_video",
            return_value={"video_id": "video-pending", "cover_id": "", "request_id": "upload-request"},
        ), patch.object(
            adq_service,
            "verify_video_in_library",
            side_effect=AdqError(
                "素材库尚未回读到视频",
                category="library_readback_pending",
                retryable=True,
                entity_id="video-pending",
            ),
        ), patch.object(adq_service, "grant_all_videos_to_mdm") as grant:
            run_adq_push("adq-readback-pending")
        with SessionLocal() as db:
            saved = db.get(AdqDelivery, "adq-readback-pending")
            self.assertEqual(saved.status, "partial")
            self.assertEqual(saved.platform_asset_id, "video-pending")
            self.assertEqual(saved.failure_stage, "library_readback")
            self.assertEqual(saved.last_error_category, "library_readback_pending")
        grant.assert_not_called()

    def test_adq_shared_task_rows_are_enriched_with_exact_video_metrics(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/adq/metrics.mp4", filename="metrics.mp4", media_type="video")
            db.add(asset)
            db.commit()
            db.add(AdqDelivery(
                id="adq-shared-metrics",
                batch_id="adq-shared-metrics",
                asset_id=asset.id,
                created_by_number="FD-A",
                account_id="80431518",
                account_name="ADQ 统一素材源账户 80431518",
                adgroup_id="__shared_library__",
                platform_asset_id="video-100",
                status="success",
                metrics_status="pending",
            ))
            db.commit()
            with patch.object(adq_service, "account_material_metrics", return_value={
                "start_date": "2026-07-19",
                "end_date": "2026-08-17",
                "videos": [{"video_id": "video-100", "metrics": {"cost_yuan": 12.3, "order_amount_yuan": 45.6, "order_roi": 3.7073}}],
            }):
                visible = adq_tasks(db=db, user=owner, page=1, page_size=10, library_only=True)

        row = visible["items"][0]
        self.assertEqual(row["metrics_status"], "success")
        self.assertEqual(row["metrics"]["cost_yuan"], 12.3)
        self.assertEqual(row["metrics_end_date"], "2026-08-17")

    def test_adq_direct_worker_uses_target_library_then_creates_unit_creative(self):
        events = []
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/adq/direct-worker.mp4", filename="direct-worker.mp4", media_type="video", size=12)
            db.add(asset)
            db.commit()
            db.add(AdqDelivery(
                id="adq-direct-worker",
                batch_id="adq-direct-batch",
                asset_id=asset.id,
                created_by_number="FD-A",
                account_id="65909064",
                account_name="目标账户",
                adgroup_id="123456",
                adgroup_name="目标营销单元",
                source_dynamic_creative_id="source-creative",
                status="pending",
            ))
            db.commit()

        def upload(_db, **kwargs):
            events.append(("upload", kwargs["account_id"]))
            return {"video_id": "video-direct", "cover_id": "cover-direct", "request_id": "upload-request"}

        def verify(_db, **kwargs):
            events.append(("verify", kwargs["account_id"], kwargs["video_id"]))
            return {"verified": True, "account_id": kwargs["account_id"], "video_id": kwargs["video_id"], "cover_id": "cover-direct"}

        def bind(_db, **kwargs):
            events.append(("bind", kwargs["account_id"], kwargs["adgroup_id"], kwargs["video_id"]))
            return {"dynamic_creative_id": "new-creative", "binding_evidence": {"verified": True}, "request_id": "bind-request"}

        with patch.object(adq_service, "upload_original_video", side_effect=upload), patch.object(
            adq_service, "verify_video_in_library", side_effect=verify,
        ), patch.object(adq_service, "add_to_adgroup", side_effect=bind):
            run_adq_push("adq-direct-worker")

        with SessionLocal() as db:
            saved = db.get(AdqDelivery, "adq-direct-worker")
            self.assertEqual(saved.status, "success")
            self.assertEqual(saved.platform_asset_id, "video-direct")
            self.assertEqual(saved.dynamic_creative_id, "new-creative")
            self.assertTrue(saved.binding_evidence["library_readback"]["verified"])
        self.assertEqual(events, [
            ("upload", "65909064"),
            ("verify", "65909064", "video-direct"),
            ("bind", "65909064", "123456", "video-direct"),
        ])

    def test_adq_marketing_unit_push_queues_verified_existing_targets(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            assets = [
                Asset(object_key=f"yxb/adq/{name}.mp4", filename=f"{name}.mp4", media_type="video", size=12)
                for name in ("a", "b")
            ]
            db.add_all(assets)
            db.commit()
            payload = AdqPushCreate(
                asset_ids=[asset.id for asset in assets],
                targets=[
                    AdqTarget(account_id="65909064", adgroup_id="1", adgroup_name="单元一"),
                    AdqTarget(account_id="65909064", adgroup_id="2", adgroup_name="单元二"),
                ],
            )
            with patch.object(adq_service, "status", return_value={"authorized": True, "message": "授权可用"}), patch.object(
                adq_service,
                "user_authorization_status",
                return_value={"authorized": True, "message": "操作人认证可用", "expires_at": None},
            ), patch.object(
                adq_service,
                "account",
                return_value={"account_id": "65909064", "account_name": "广州慕可生物科技有限公司"},
            ), patch.object(
                adq_service,
                "resolve_target",
                side_effect=lambda _db, account_id, adgroup_id, source_dynamic_creative_id="": {
                    "account_id": account_id,
                    "adgroup_id": adgroup_id,
                    "adgroup_name": f"单元{adgroup_id}",
                    "source_dynamic_creative_id": f"creative-{adgroup_id}",
                },
            ):
                result = adq_push(payload, db, owner)
            saved = db.scalars(select(AdqDelivery).where(AdqDelivery.created_by_number == "FD-A")).all()
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["asset_count"], 2)
        self.assertEqual(result["target_count"], 2)
        self.assertEqual(len(saved), 4)
        self.assertEqual({row.adgroup_id for row in saved}, {"1", "2"})
        self.assertEqual({row.account_name for row in saved}, {"广州慕可生物科技有限公司"})

    def test_adq_marketing_unit_push_requires_operator_identity_before_queue(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/adq/auth-required.mp4", filename="auth-required.mp4", media_type="video", size=12)
            db.add(asset)
            db.commit()
            payload = AdqPushCreate(
                asset_ids=[asset.id],
                targets=[AdqTarget(account_id="65909064", adgroup_id="1", adgroup_name="单元一")],
            )
            with patch.object(adq_service, "status", return_value={"authorized": True, "message": "授权可用"}), patch.object(
                adq_service,
                "user_authorization_status",
                return_value={"authorized": False, "message": "创建投放创意前需完成一次 ADQ 操作人实名认证", "expires_at": None},
            ):
                with self.assertRaises(HTTPException) as caught:
                    adq_push(payload, db, owner)
            saved = db.scalars(select(AdqDelivery).where(AdqDelivery.asset_id == asset.id)).all()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("操作人实名认证", caught.exception.detail)
        self.assertEqual(saved, [])

    def test_adq_tasks_are_private_and_batch_retry_preserves_success(self):
        owner = {"number": "FD-A", "realName": "甲"}
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/adq/retry.mp4", filename="retry.mp4", media_type="video", size=12)
            db.add(asset)
            db.commit()
            db.add_all([
                AdqDelivery(id="adq-success", batch_id="adq-batch", asset_id=asset.id, created_by_number="FD-A", account_id="65909064", adgroup_id="1", platform_asset_id="video-1", status="success"),
                AdqDelivery(id="adq-failed", batch_id="adq-batch", asset_id=asset.id, created_by_number="FD-A", account_id="65909064", adgroup_id="2", platform_asset_id="video-1", status="partial"),
                AdqDelivery(id="adq-foreign", batch_id="adq-batch", asset_id=asset.id, created_by_number="FD-B", account_id="65909064", adgroup_id="3", status="failed"),
            ])
            db.commit()
            visible = adq_tasks(db=db, user=owner, page=1, page_size=10)
            with patch.object(
                adq_service,
                "user_authorization_status",
                return_value={"authorized": True, "message": "操作人认证可用", "expires_at": None},
            ):
                result = adq_batch_retry_failed("adq-batch", db, owner)
            db.refresh(db.get(AdqDelivery, "adq-success"))
        self.assertEqual(visible["total"], 2)
        self.assertEqual(result["queued_task_ids"], ["adq-failed"])
        self.assertEqual(result["preserved_success_count"], 1)


if __name__ == "__main__":
    unittest.main()
    admin_access_grant_create,
    admin_access_grant_revoke,
    admin_access_grants,
