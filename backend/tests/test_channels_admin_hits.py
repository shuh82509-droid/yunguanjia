import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.database import Base, SessionLocal, engine
from app.main import (
    PushPreferenceToggle,
    _asset_gmv_summary,
    _active_channels_account_for_task,
    _apply_channels_readback,
    _readback_channels_tasks,
    _is_operation_audit_noise,
    _operation_action,
    _seed_protected_admin_grants,
    channels_push,
    channels_promotion_order_create,
    channels_promotion_orders,
    channels_promotion_payment_session_create,
    channels_promotion_payment_session_event,
    channels_promotion_payment_session_get,
    channels_promotion_quote,
    claim_channels_tasks,
    claim_qianchuan_tasks,
    channels_task_cancel,
    channels_task_retry,
    channels_task_edit,
    channels_tasks,
    adq_tasks,
    ensure_asset_schema,
    is_operation_admin,
    is_permission_manager,
    is_super_admin,
    list_assets,
    operation_logs,
    qianchuan_tasks,
    qianchuan_task_cancel,
    run_qianchuan_push,
    run_channels_confirmation_if_due,
    run_channels_daily_metrics_if_due,
    run_channels_push,
    push_preferences,
    toggle_push_preference,
    user_permissions,
)
from app.channels_service import ChannelsCancelled, ChannelsError, ChannelsService
from app.channels_promotion_service import (
    ChannelsPromotionError,
    ChannelsPromotionService,
    PROMOTION_DURATIONS,
    PROMOTION_TARGETS,
    channels_promotion_service,
)
from app.models import (
    AdminGrant,
    AdqDelivery,
    AppMeta,
    Asset,
    AssetFavorite,
    ChannelsAccount,
    ChannelsDelivery,
    ChannelsPromotionAccount,
    ChannelsPromotionOrder,
    ChannelsPromotionPaymentSession,
    OperationLog,
    PushPreference,
    QianchuanDelivery,
    QianchuanMetricDaily,
    ChannelsMetricDaily,
)
from app.schemas import (
    ChannelsPromotionOrderCreate,
    ChannelsPromotionPaymentEvent,
    ChannelsPromotionPaymentSessionCreate,
    ChannelsPromotionQuote,
    ChannelsPushCreate,
    ChannelsTaskEdit,
)


class ChannelsAdminHitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        ensure_asset_schema()

    def setUp(self):
        with SessionLocal() as db:
            db.query(ChannelsPromotionPaymentSession).delete()
            db.query(OperationLog).delete()
            db.query(PushPreference).delete()
            db.query(AssetFavorite).delete()
            db.query(ChannelsPromotionOrder).delete()
            db.query(ChannelsPromotionAccount).delete()
            db.query(ChannelsMetricDaily).delete()
            db.query(ChannelsDelivery).delete()
            db.query(ChannelsAccount).delete()
            db.query(QianchuanMetricDaily).delete()
            db.query(QianchuanDelivery).delete()
            db.query(AdminGrant).delete()
            db.query(Asset).delete()
            db.commit()

    @staticmethod
    def user(number: str, name: str) -> dict:
        return {"number": number, "realName": name, "groupName": "品牌营销部", "status": "normal"}

    def test_edit_failed_publish_preserves_task_identity_and_review_gate(self):
        owner = self.user('FD-EDIT','发布人')
        with SessionLocal() as db:
            asset = Asset(object_key='yxb/edit/a.mp4',filename='a.mp4',media_type='video')
            account = ChannelsAccount(id='edit-account',owner_number='FD-EDIT',nickname='测试账号',auth_file='/data/test',status='active')
            db.add_all([asset,account]); db.flush()
            task = ChannelsDelivery(id='edit-task',batch_id='original-batch',asset_id=asset.id,
                account_id=account.id,created_by_number='FD-EDIT',status='failed',failure_stage='internal_preflight',video_annotation='ai_generated')
            db.add(task); db.commit()
            data = ChannelsTaskEdit(title='这是修改后的标题',product_id='new-product',product_name='指定商品')
            result = channels_task_edit(task.id,data,db,owner)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(task.title,'这是修改后的标题')
            self.assertEqual(task.product_id,'new-product')
            self.assertEqual(task.video_annotation,'ai_generated')
            self.assertEqual(task.batch_id,'original-batch')
            with self.assertRaises(HTTPException) as forbidden:
                channels_task_edit(task.id,data,db,self.user('FD-OTHER','其他人'))
            self.assertEqual(forbidden.exception.status_code,404)
            data.retry=True
            with patch('app.main._require_assets_review_approved',side_effect=HTTPException(409,'未通过审核')):
                with self.assertRaises(HTTPException): channels_task_edit(task.id,data,db,owner)
            self.assertEqual(task.status,'failed')
            other = ChannelsDelivery(id='other-task',batch_id='other-batch',asset_id=asset.id,
                account_id=account.id,created_by_number='FD-EDIT',status='submitted')
            db.add(other); db.commit()
            with patch('app.main._require_assets_review_approved'):
                for retry in (lambda: channels_task_edit(task.id,data,db,owner),
                              lambda: channels_task_retry(task.id,db,owner)):
                    with self.assertRaises(HTTPException) as duplicate:
                        retry()
                    self.assertEqual(duplicate.exception.status_code,409)
            db.delete(other); db.commit()
            with patch('app.main._require_assets_review_approved'), patch('app.main._validated_reference_images',
                return_value=[{'object_key':'yxb/references/new-cover.jpg','filename':'新封面.jpg'}]):
                data.cover_object_key='yxb/references/new-cover.jpg'; data.cover_filename='新封面.jpg'
                result = channels_task_edit(task.id,data,db,owner)
            self.assertEqual(result['status'],'pending')
            self.assertEqual(task.cover_filename,'新封面.jpg')
            self.assertEqual(task.cover_object_key,'yxb/references/new-cover.jpg')
            self.assertEqual(db.query(ChannelsDelivery).count(),1)
            task.status='failed'
            task.publish_clicked_at=datetime.utcnow(); db.commit()
            with self.assertRaises(HTTPException) as submitted:
                channels_task_edit(task.id,data,db,owner)
            self.assertEqual(submitted.exception.status_code,409)

    def test_only_protected_owners_manage_permissions_and_delegates_only_view_logs(self):
        with patch.dict(os.environ, {"SUPER_ADMIN_NAMES": "舒豪,吴为", "SUPER_ADMIN_NUMBERS": ""}, clear=False):
            self.assertTrue(is_super_admin(self.user("FD-026222", "舒豪")))
            self.assertTrue(is_super_admin(self.user("FD-000002", "吴为")))
            self.assertFalse(is_super_admin(self.user("FD-000003", "普通管理员")))
            with SessionLocal() as db:
                _seed_protected_admin_grants(db)
                db.add(AdminGrant(
                    identifier="name:普通管理员",
                    real_name="普通管理员",
                    role="operation_admin",
                    active=True,
                ))
                db.commit()
            delegated = self.user("FD-000003", "普通管理员")
            self.assertTrue(is_operation_admin(delegated))
            self.assertEqual(user_permissions(delegated)["manage_permissions"], False)
            self.assertEqual(user_permissions(delegated)["operation_admin"], True)

    def test_permission_manager_is_highest_grantable_role(self):
        with SessionLocal() as db:
            db.add(AdminGrant(
                identifier="name:权限负责人",
                real_name="权限负责人",
                role="permission_manager",
                active=True,
            ))
            db.commit()
        delegated = self.user("FD-000004", "权限负责人")
        self.assertTrue(is_permission_manager(delegated))
        self.assertTrue(is_operation_admin(delegated))
        self.assertTrue(user_permissions(delegated)["manage_permissions"])

    def test_operation_audit_hides_transport_chatter_but_keeps_business_outcomes(self):
        self.assertTrue(_is_operation_audit_noise("/api/uploads/part-leases/acquire", 200))
        self.assertTrue(_is_operation_audit_noise("/api/uploads/multipart/sessions/s-1/parts", 200))
        self.assertTrue(_is_operation_audit_noise("/api/uploads/multipart/sessions/s-1/complete", 200))
        self.assertFalse(_is_operation_audit_noise("/api/uploads/multipart/sessions/s-1/complete", 502))
        self.assertEqual(_operation_action("POST", "/api/uploads/multipart/sessions", "success"), "开始上传")
        self.assertEqual(_operation_action("POST", "/api/uploads/complete", "success"), "上传完成")
        self.assertEqual(_operation_action("DELETE", "/api/uploads/multipart/sessions/s-1", "success"), "取消上传")

        with SessionLocal() as db:
            db.add_all([
                OperationLog(actor_name="叶家炜", module="素材上传", action="POST acquire", method="POST", path="/api/uploads/part-leases/acquire", result="success", status_code=200),
                OperationLog(actor_name="叶家炜", module="素材上传", action="POST parts", method="POST", path="/api/uploads/multipart/sessions/s-1/parts", result="success", status_code=200),
                OperationLog(actor_name="叶家炜", module="素材上传", action="POST complete", method="POST", path="/api/uploads/multipart/sessions/s-1/complete", result="success", status_code=200),
                OperationLog(actor_name="叶家炜", module="素材上传", action="POST complete", method="POST", path="/api/uploads/multipart/sessions/s-2/complete", result="failed", status_code=502),
                OperationLog(actor_name="叶家炜", module="素材上传", action="POST sessions", method="POST", path="/api/uploads/multipart/sessions", result="success", status_code=200),
                OperationLog(actor_name="叶家炜", module="素材上传", action="POST complete", method="POST", path="/api/uploads/complete", result="success", status_code=200),
            ])
            db.commit()
            with patch.dict(os.environ, {"SUPER_ADMIN_NAMES": "舒豪,吴为"}, clear=False):
                result = operation_logs(
                    q="叶家炜",
                    module="",
                    result="",
                    page=1,
                    page_size=20,
                    db=db,
                    user=self.user("FD-026222", "舒豪"),
                )
            self.assertEqual(result["total"], 3)
            self.assertEqual(
                {item["action"] for item in result["items"]},
                {"开始上传", "上传完成", "上传失败"},
            )

    def test_operation_logs_support_china_date_range_and_reject_non_admin(self):
        with SessionLocal() as db:
            db.add_all([
                OperationLog(
                    actor_name="测试管理员",
                    module="权限管理",
                    action="开通权限",
                    method="POST",
                    path="/api/admin/access-grants",
                    result="success",
                    status_code=200,
                    created_at=datetime(2026, 8, 20, 15, 59, 59),
                ),
                OperationLog(
                    actor_name="测试管理员",
                    module="权限管理",
                    action="取消权限",
                    method="DELETE",
                    path="/api/admin/access-grants/1",
                    result="success",
                    status_code=200,
                    created_at=datetime(2026, 8, 20, 16, 0, 0),
                ),
            ])
            db.commit()
            with patch.dict(os.environ, {"SUPER_ADMIN_NAMES": "舒豪,吴为"}, clear=False):
                result = operation_logs(
                    q="",
                    module="权限管理",
                    result="",
                    date_from=date(2026, 8, 21),
                    date_to=date(2026, 8, 21),
                    page=1,
                    page_size=20,
                    db=db,
                    user=self.user("FD-026222", "舒豪"),
                )
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["path"], "/api/admin/access-grants/1")

                with self.assertRaises(HTTPException) as error:
                    operation_logs(
                        q="",
                        module="",
                        result="",
                        date_from=None,
                        date_to=None,
                        page=1,
                        page_size=20,
                        db=db,
                        user=self.user("FD-000004", "普通成员"),
                    )
                self.assertEqual(error.exception.status_code, 403)

    def test_hit_gmv_deduplicates_retry_rows_and_requires_verified_daily_data(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/hit.mp4", filename="hit.mp4", media_type="video", size=1)
            db.add(asset)
            db.flush()
            for task_id in ("retry-a", "retry-b"):
                db.add(QianchuanDelivery(
                    id=task_id,
                    batch_id="batch",
                    asset_id=asset.id,
                    created_by_number="FD-1",
                    advertiser_id="100",
                    plan_id="200",
                    platform_asset_id="video-1",
                    status="success",
                ))
            db.flush()
            db.add_all([
                QianchuanMetricDaily(task_id="retry-a", advertiser_id="100", plan_id="200", video_id="video-1", stat_date="2026-08-09", metrics={"pay_order_amount": 30000}, status="success", has_data=True, link_verified=True),
                QianchuanMetricDaily(task_id="retry-b", advertiser_id="100", plan_id="200", video_id="video-1", stat_date="2026-08-09", metrics={"pay_order_amount": 40000}, status="success", has_data=True, link_verified=True),
                QianchuanMetricDaily(task_id="retry-a", advertiser_id="100", plan_id="200", video_id="video-1", stat_date="2026-08-10", metrics={"pay_order_amount": 11000}, status="success", has_data=True, link_verified=True),
                QianchuanMetricDaily(task_id="retry-b", advertiser_id="100", plan_id="200", video_id="video-1", stat_date="2026-08-11", metrics={"pay_order_amount": 999999}, status="success", has_data=True, link_verified=False),
            ])
            db.commit()
            summary = _asset_gmv_summary(db)
            self.assertEqual(summary[asset.id]["gmv_yuan"], 51000)
            self.assertEqual(summary[asset.id]["target_count"], 1)
            db.add(Asset(object_key="yxb/unknown.mp4", filename="unknown.mp4", media_type="video", size=1))
            db.commit()
            page = list_assets(
                q="", category="", content_type="", status="", media_type="",
                asset_scope="", library_type="", asset_subtype="", ingest_source="",
                directory="", favorite=None, hot_only=False, sort="gmv_desc",
                page=1, page_size=24, db=db, user=self.user("FD-1", "甲"),
            )
            self.assertEqual(page.items[0].id, asset.id)
            self.assertEqual(page.items[0].historical_gmv_yuan, 51000)
            self.assertIsNone(page.items[-1].historical_gmv_yuan)

    def test_channels_accounts_and_tasks_are_private_per_oa_user(self):
        user_one = self.user("FD-1", "甲")
        user_two = self.user("FD-2", "乙")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/video.mp4", filename="video.mp4", media_type="video", size=1024)
            db.add(asset)
            db.flush()
            db.add_all([
                ChannelsAccount(id="account-1", owner_number="FD-1", owner_name="甲", nickname="甲的视频号", auth_file="/data/channels-auth/1.json", status="active", authorized_at=datetime.utcnow()),
                ChannelsAccount(id="account-2", owner_number="FD-2", owner_name="乙", nickname="乙的视频号", auth_file="/data/channels-auth/2.json", status="active", authorized_at=datetime.utcnow()),
            ])
            db.commit()
            with patch("app.main.oss_service.head_asset", return_value={"media_type": "image", "size": 2048}):
                result = channels_push(ChannelsPushCreate(
                    asset_ids=[asset.id],
                    account_ids=["account-1"],
                    product_id="10001206069243",
                    product_name="WIS隐形水润面膜",
                    covers=[{
                        "asset_id": asset.id,
                        "object_key": "yxb/references/甲/video-cover.webp",
                        "filename": "video-cover.webp",
                    }],
                    annotations=[{
                        "asset_id": asset.id,
                        "annotation": "ai_generated",
                    }],
                ), db=db, user=user_one)
            self.assertEqual(len(result["task_ids"]), 1)
            created = db.get(ChannelsDelivery, result["task_ids"][0])
            self.assertEqual(created.product_id, "10001206069243")
            self.assertEqual(created.product_name, "WIS隐形水润面膜")
            self.assertEqual(created.cover_object_key, "yxb/references/甲/video-cover.webp")
            self.assertEqual(created.cover_filename, "video-cover.webp")
            self.assertEqual(created.video_annotation, "ai_generated")
            self.assertEqual(channels_tasks(q="", status="all", page=1, page_size=10, db=db, user=user_one)["items"][0]["video_annotation_label"], "含AI生成内容")
            private_one = channels_tasks(q="", status="all", page=1, page_size=10, db=db, user=user_one)
            private_two = channels_tasks(q="", status="all", page=1, page_size=10, db=db, user=user_two)
            self.assertEqual(private_one["total"], 1)
            self.assertEqual(private_two["total"], 0)
            with self.assertRaises(Exception):
                channels_push(ChannelsPushCreate(asset_ids=[asset.id], account_ids=["account-2"]), db=db, user=user_one)

    def test_channels_batch_push_keeps_one_title_for_each_video(self):
        user = self.user("FD-1", "甲")
        with SessionLocal() as db:
            first = Asset(object_key="yxb/first.mp4", filename="first.mp4", media_type="video", size=1024)
            second = Asset(object_key="yxb/second.mp4", filename="second.mp4", media_type="video", size=1024)
            account = ChannelsAccount(
                id="title-account", owner_number="FD-1", owner_name="甲",
                nickname="甲的视频号", auth_file="/data/channels-auth/title.json", status="active",
            )
            db.add_all([first, second, account])
            db.commit()
            result = channels_push(ChannelsPushCreate(
                asset_ids=[first.id, second.id],
                account_ids=[account.id],
                title="旧版共用标题",
                titles=[
                    {"asset_id": first.id, "title": "第一条独立标题"},
                    {"asset_id": second.id, "title": "第二条独立标题"},
                ],
            ), db=db, user=user)
            created = db.scalars(
                select(ChannelsDelivery).where(ChannelsDelivery.id.in_(result["task_ids"])).order_by(ChannelsDelivery.asset_id)
            ).all()
            self.assertEqual([item.title for item in created], ["第一条独立标题", "第二条独立标题"])

    def test_channels_push_blocks_same_asset_account_while_prior_task_is_unconfirmed(self):
        user = self.user("FD-1", "甲")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/deduplicate.mp4", filename="deduplicate.mp4", media_type="video", size=1024)
            account = ChannelsAccount(
                id="deduplicate-account", owner_number="FD-1", owner_name="甲",
                nickname="甲的视频号", auth_file="/data/channels-auth/deduplicate.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            existing = ChannelsDelivery(
                id="deduplicate-existing", batch_id="deduplicate-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="已受理待确认", status="submitted",
                submitted_at=datetime.utcnow(),
            )
            db.add(existing)
            db.commit()

            result = channels_push(
                ChannelsPushCreate(asset_ids=[asset.id], account_ids=[account.id]), db=db, user=user,
            )

            self.assertEqual(result["status"], "duplicate_blocked")
            self.assertEqual(result["task_ids"], [])
            self.assertEqual(result["duplicate_task_ids"], [existing.id])
            self.assertEqual(len(db.scalars(
                select(ChannelsDelivery).where(ChannelsDelivery.asset_id == asset.id)
            ).all()), 1)

    def test_channels_push_defaults_known_ai_asset_to_ai_annotation(self):
        user = self.user("FD-1", "甲")
        with SessionLocal() as db:
            asset = Asset(
                object_key="yxb/ai-output.mp4", filename="数字人成片.mp4", media_type="video", size=1024,
                asset_subtype="AI混剪成片",
            )
            account = ChannelsAccount(
                id="ai-account", owner_number="FD-1", owner_name="甲",
                nickname="甲的视频号", auth_file="/data/channels-auth/ai.json", status="active",
            )
            db.add_all([asset, account])
            db.commit()
            result = channels_push(
                ChannelsPushCreate(asset_ids=[asset.id], account_ids=[account.id]),
                db=db,
                user=user,
            )
            created = db.get(ChannelsDelivery, result["task_ids"][0])
            self.assertEqual(created.video_annotation, "ai_generated")

    def test_channels_self_shot_annotation_requires_time_and_location(self):
        user = self.user("FD-1", "甲")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/self-shot.mp4", filename="实拍.mp4", media_type="video", size=1024)
            account = ChannelsAccount(
                id="self-shot-account", owner_number="FD-1", owner_name="甲",
                nickname="甲的视频号", auth_file="/data/channels-auth/self-shot.json", status="active",
            )
            db.add_all([asset, account])
            db.commit()
            with self.assertRaises(HTTPException) as raised:
                channels_push(ChannelsPushCreate(
                    asset_ids=[asset.id], account_ids=[account.id],
                    annotations=[{"asset_id": asset.id, "annotation": "self_shot"}],
                ), db=db, user=user)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("拍摄时间和地点", raised.exception.detail)

    def test_operation_admin_sees_all_delivery_data_but_cannot_manage_foreign_tasks(self):
        owner_one = self.user("FD-1", "甲")
        owner_two = self.user("FD-2", "乙")
        manager = self.user("FD-ADMIN", "数据管理员")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/all-data.mp4", filename="全员数据.mp4", media_type="video", size=1)
            db.add(asset)
            db.flush()
            db.add(AdminGrant(identifier="name:数据管理员", real_name="数据管理员", role="operation_admin", active=True))
            db.add_all([
                ChannelsAccount(id="admin-view-account-1", owner_number="FD-1", owner_name="甲", nickname="甲视频号", auth_file="/data/channels-auth/admin-1.json", status="active"),
                ChannelsAccount(id="admin-view-account-2", owner_number="FD-2", owner_name="乙", nickname="乙视频号", auth_file="/data/channels-auth/admin-2.json", status="active"),
                QianchuanDelivery(id="qc-1", batch_id="qc-b1", asset_id=asset.id, created_by_number="FD-1", created_by_name="甲", advertiser_id="100", status="success"),
                QianchuanDelivery(id="qc-2", batch_id="qc-b2", asset_id=asset.id, created_by_number="FD-2", created_by_name="乙", advertiser_id="200", status="success"),
                AdqDelivery(id="adq-1", batch_id="adq-b1", asset_id=asset.id, created_by_number="FD-1", created_by_name="甲", account_id="300", adgroup_id="301", status="success"),
                AdqDelivery(id="adq-2", batch_id="adq-b2", asset_id=asset.id, created_by_number="FD-2", created_by_name="乙", account_id="400", adgroup_id="401", status="success"),
            ])
            db.flush()
            db.add_all([
                ChannelsDelivery(id="ch-1", batch_id="ch-b1", asset_id=asset.id, created_by_number="FD-1", created_by_name="甲", account_id="admin-view-account-1", account_name="甲视频号", status="success"),
                ChannelsDelivery(id="ch-2", batch_id="ch-b2", asset_id=asset.id, created_by_number="FD-2", created_by_name="乙", account_id="admin-view-account-2", account_name="乙视频号", status="success"),
            ])
            db.commit()

            personal_qc = qianchuan_tasks(asset_id=None, limit=None, q="", status="all", page=1, page_size=10, db=db, user=owner_one)
            admin_qc = qianchuan_tasks(asset_id=None, limit=None, q="", status="all", page=1, page_size=10, db=db, user=manager)
            personal_adq = adq_tasks(asset_id=None, q="", status="all", page=1, page_size=10, db=db, user=owner_one)
            admin_adq = adq_tasks(asset_id=None, q="", status="all", page=1, page_size=10, db=db, user=manager)
            personal_channels = channels_tasks(q="", status="all", page=1, page_size=10, db=db, user=owner_one)
            admin_channels = channels_tasks(q="", status="all", page=1, page_size=10, db=db, user=manager)
            admin_own_channels = channels_tasks(q="", status="all", scope="mine", page=1, page_size=10, db=db, user=manager)

            self.assertEqual(personal_qc["total"], 1)
            self.assertEqual(personal_qc["viewer_scope"], "personal")
            self.assertEqual(admin_qc["total"], 2)
            self.assertEqual(admin_qc["viewer_scope"], "all")
            self.assertTrue(all(not item["can_manage"] for item in admin_qc["items"]))
            self.assertEqual(personal_adq["total"], 1)
            self.assertEqual(admin_adq["total"], 2)
            self.assertEqual(admin_adq["viewer_scope"], "all")
            self.assertTrue(all(not item["can_manage"] for item in admin_adq["items"]))
            self.assertEqual(personal_channels["total"], 1)
            self.assertEqual(admin_channels["total"], 2)
            self.assertEqual(admin_channels["viewer_scope"], "all")
            self.assertTrue(all(not item["can_manage"] for item in admin_channels["items"]))
            self.assertEqual(admin_own_channels["total"], 0)
            self.assertEqual(admin_own_channels["viewer_scope"], "personal")
            self.assertTrue(admin_own_channels["can_view_all"])

    def test_push_cancellation_is_available_to_owner_and_operation_admin(self):
        owner = self.user("FD-CANCEL", "发起人")
        manager = self.user("FD-ADMIN", "取消管理员")
        stranger = self.user("FD-OTHER", "其他人")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cancel/original.mp4", filename="待取消.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="cancel-account", owner_number="FD-CANCEL", owner_name="发起人",
                nickname="待取消视频号", auth_file="/data/cancel.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add(AdminGrant(identifier="name:取消管理员", real_name="取消管理员", role="operation_admin", active=True))
            db.add_all([
                QianchuanDelivery(
                    id="cancel-qc", batch_id="cancel-qc-batch", asset_id=asset.id,
                    created_by_number="FD-CANCEL", created_by_name="发起人",
                    advertiser_id="1001", plan_id="2001", plan_type="multiplication",
                    status="pending", message="等待中",
                ),
                ChannelsDelivery(
                    id="cancel-channels", batch_id="cancel-channels-batch", asset_id=asset.id,
                    created_by_number="FD-CANCEL", created_by_name="发起人",
                    account_id=account.id, account_name=account.nickname,
                    status="pending", message="等待中",
                ),
            ])
            db.commit()

            manager_qc = qianchuan_tasks(asset_id=None, limit=None, q="", status="all", page=1, page_size=10, db=db, user=manager)
            manager_channels = channels_tasks(q="", status="all", page=1, page_size=10, db=db, user=manager)
            self.assertTrue(manager_qc["items"][0]["can_cancel"])
            self.assertTrue(manager_channels["items"][0]["can_cancel"])

            with self.assertRaises(HTTPException) as denied:
                qianchuan_task_cancel("cancel-qc", db, stranger)
            self.assertEqual(denied.exception.status_code, 404)

            qc_result = qianchuan_task_cancel("cancel-qc", db, manager)
            channels_result = channels_task_cancel("cancel-channels", db, owner)
            self.assertEqual(qc_result["status"], "cancelled")
            self.assertEqual(channels_result["status"], "cancelled")
            self.assertIn("未向千川发送", qc_result["message"])
            self.assertIn("未点击视频号", channels_result["message"])

    def test_push_cancellation_never_overwrites_platform_accepted_state(self):
        owner = self.user("FD-CANCEL", "发起人")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cancel/late.mp4", filename="平台已受理.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="cancel-late-account", owner_number="FD-CANCEL", owner_name="发起人",
                nickname="已受理视频号", auth_file="/data/cancel-late.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add_all([
                QianchuanDelivery(
                    id="cancel-qc-success", batch_id="cancel-qc-success-batch", asset_id=asset.id,
                    created_by_number="FD-CANCEL", created_by_name="发起人",
                    advertiser_id="1001", plan_id="2001", platform_asset_id="video-1",
                    binding_verified_at=datetime.utcnow(), status="success",
                ),
                ChannelsDelivery(
                    id="cancel-channels-submitted", batch_id="cancel-channels-submitted-batch", asset_id=asset.id,
                    created_by_number="FD-CANCEL", created_by_name="发起人",
                    account_id=account.id, account_name=account.nickname,
                    status="submitted", submitted_at=datetime.utcnow(), failure_stage="confirm_publish",
                ),
            ])
            db.commit()
            with self.assertRaises(HTTPException) as qc_late:
                qianchuan_task_cancel("cancel-qc-success", db, owner)
            with self.assertRaises(HTTPException) as channels_late:
                channels_task_cancel("cancel-channels-submitted", db, owner)
            self.assertEqual(qc_late.exception.status_code, 409)
            self.assertEqual(channels_late.exception.status_code, 409)
            self.assertEqual(db.get(QianchuanDelivery, "cancel-qc-success").status, "success")
            self.assertEqual(db.get(ChannelsDelivery, "cancel-channels-submitted").status, "submitted")

    def test_channels_worker_stops_at_cooperative_boundary_before_publish_click(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cancel/running.mp4", filename="发布中取消.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="cancel-running-account", owner_number="FD-CANCEL", owner_name="发起人",
                nickname="发布中视频号", auth_file="/data/cancel-running.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add(ChannelsDelivery(
                id="cancel-running-task", batch_id="cancel-running-batch", asset_id=asset.id,
                created_by_number="FD-CANCEL", created_by_name="发起人",
                account_id=account.id, account_name=account.nickname, title="发布中取消",
                status="pending",
            ))
            db.commit()

        def cooperative_publish(**kwargs):
            with SessionLocal() as db:
                task = db.get(ChannelsDelivery, "cancel-running-task")
                task.status = "cancel_requested"
                task.message = "用户请求取消"
                db.commit()
            self.assertTrue(kwargs["should_cancel"]())
            raise ChannelsCancelled("已按要求停止发布；尚未点击视频号平台的发布按钮", "upload_original")

        with patch("app.main.channels_service.publish", side_effect=cooperative_publish):
            run_channels_push("cancel-running-task")
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "cancel-running-task")
            self.assertEqual(task.status, "cancelled")
            self.assertIsNone(task.submitted_at)
            self.assertEqual(task.failure_stage, "")
            self.assertIn("尚未点击", task.message)

    def test_qianchuan_worker_honours_active_cancel_without_starting_platform_work(self):
        owner = self.user("FD-CANCEL", "发起人")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cancel/qc-running.mp4", filename="千川上传中取消.mp4", media_type="video", size=1)
            db.add(asset)
            db.flush()
            db.add(QianchuanDelivery(
                id="cancel-qc-running", batch_id="cancel-qc-running-batch", asset_id=asset.id,
                created_by_number="FD-CANCEL", created_by_name="发起人",
                advertiser_id="1001", plan_id="2001", plan_type="multiplication",
                status="uploading", message="上传中",
            ))
            db.commit()
            result = qianchuan_task_cancel("cancel-qc-running", db, owner)
            self.assertEqual(result["status"], "cancel_requested")

        with patch("app.main.qianchuan_service.begin_video_upload") as begin_upload, patch("app.main.qianchuan_service.add_to_plan") as add_to_plan:
            self.assertEqual(claim_qianchuan_tasks(1), [])
            run_qianchuan_push(["cancel-qc-running"])
            begin_upload.assert_not_called()
            add_to_plan.assert_not_called()
        with SessionLocal() as db:
            task = db.get(QianchuanDelivery, "cancel-qc-running")
            self.assertEqual(task.status, "cancelled")
            self.assertIn("停止", task.message)

    def test_channels_cancel_request_is_settled_after_worker_restart(self):
        owner = self.user("FD-CANCEL", "发起人")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cancel/restart.mp4", filename="重启前取消.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="cancel-restart-account", owner_number="FD-CANCEL", owner_name="发起人",
                nickname="重启取消视频号", auth_file="/data/cancel-restart.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add(ChannelsDelivery(
                id="cancel-restart-task", batch_id="cancel-restart-batch", asset_id=asset.id,
                created_by_number="FD-CANCEL", created_by_name="发起人",
                account_id=account.id, account_name=account.nickname, title="重启前取消",
                status="publishing", failure_stage="download_cover",
            ))
            db.commit()
            result = channels_task_cancel("cancel-restart-task", db, owner)
            self.assertEqual(result["status"], "cancel_requested")

        self.assertEqual(claim_channels_tasks(1), [])
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "cancel-restart-task")
            self.assertEqual(task.status, "cancelled")
            self.assertEqual(task.failure_stage, "")
            self.assertIn("尚未点击", task.message)

    def test_asset_upload_and_all_push_lists_filter_by_shanghai_calendar_day(self):
        owner = self.user("FD-DATE", "日期测试")
        before_boundary = datetime(2026, 8, 24, 15, 59, 59)
        inside_day = datetime(2026, 8, 24, 16, 0, 0)
        with SessionLocal() as db:
            old_asset = Asset(object_key="yxb/date/old.mp4", filename="old.mp4", media_type="video", modified_at=before_boundary)
            day_asset = Asset(object_key="yxb/date/day.mp4", filename="day.mp4", media_type="video", modified_at=inside_day)
            account = ChannelsAccount(id="date-account", owner_number="FD-DATE", owner_name="日期测试", nickname="日期号", auth_file="/data/date.json", status="active")
            db.add_all([old_asset, day_asset, account])
            db.flush()
            db.add_all([
                QianchuanDelivery(id="date-qc-old", batch_id="date-qc-b1", asset_id=old_asset.id, created_by_number="FD-DATE", created_by_name="日期测试", advertiser_id="1", status="success", created_at=before_boundary),
                QianchuanDelivery(id="date-qc-day", batch_id="date-qc-b2", asset_id=day_asset.id, created_by_number="FD-DATE", created_by_name="日期测试", advertiser_id="1", status="success", created_at=inside_day),
                AdqDelivery(id="date-adq-old", batch_id="date-adq-b1", asset_id=old_asset.id, created_by_number="FD-DATE", created_by_name="日期测试", account_id="1", adgroup_id="1", status="success", created_at=before_boundary),
                AdqDelivery(id="date-adq-day", batch_id="date-adq-b2", asset_id=day_asset.id, created_by_number="FD-DATE", created_by_name="日期测试", account_id="1", adgroup_id="1", status="success", created_at=inside_day),
                ChannelsDelivery(id="date-ch-old", batch_id="date-ch-b1", asset_id=old_asset.id, created_by_number="FD-DATE", created_by_name="日期测试", account_id=account.id, account_name=account.nickname, status="success", created_at=before_boundary),
                ChannelsDelivery(id="date-ch-day", batch_id="date-ch-b2", asset_id=day_asset.id, created_by_number="FD-DATE", created_by_name="日期测试", account_id=account.id, account_name=account.nickname, status="success", created_at=inside_day),
            ])
            db.commit()

            assets = list_assets(
                q="", category="", content_type="", status="", media_type="", asset_scope="",
                library_type="", asset_subtype="", folder_name="", ingest_source="", mine_only=False,
                directory="", favorite=None, hot_only=False, effective_only=False,
                upload_start_date=date(2026, 8, 25), upload_end_date=date(2026, 8, 25),
                sort="newest", page=1, page_size=24, db=db, user=owner,
            )
            qc = qianchuan_tasks(asset_id=None, limit=None, q="", status="all", page=1, page_size=10, push_start_date=date(2026, 8, 25), push_end_date=date(2026, 8, 25), db=db, user=owner)
            adq = adq_tasks(asset_id=None, q="", status="all", page=1, page_size=10, library_only=False, push_start_date=date(2026, 8, 25), push_end_date=date(2026, 8, 25), db=db, user=owner)
            channels = channels_tasks(q="", status="all", scope="mine", page=1, page_size=10, push_start_date=date(2026, 8, 25), push_end_date=date(2026, 8, 25), db=db, user=owner)

            self.assertEqual([item.filename for item in assets.items], ["day.mp4"])
            self.assertEqual([item["id"] for item in qc["items"]], ["date-qc-day"])
            self.assertEqual([item["id"] for item in adq["items"]], ["date-adq-day"])
            self.assertEqual([item["id"] for item in channels["items"]], ["date-ch-day"])

    def test_push_preferences_are_private_and_support_personal_pins(self):
        user_one = self.user("FD-1", "甲")
        user_two = self.user("FD-2", "乙")
        payload = PushPreferenceToggle(
            account_id="1001",
            account_name="WIS账户",
            target_id="2001",
            target_name="乘方计划",
            target_type="multiplication",
        )
        with SessionLocal() as db:
            pinned = toggle_push_preference("qianchuan", payload, db, user_one)
            self.assertTrue(pinned["pinned"])
            self.assertEqual(push_preferences("qianchuan", db, user_one)["total"], 1)
            self.assertEqual(push_preferences("qianchuan", db, user_two)["total"], 0)
            unpinned = toggle_push_preference("qianchuan", payload, db, user_one)
            self.assertFalse(unpinned["pinned"])

    def test_asset_list_exposes_team_favorite_count_and_sorts_by_it(self):
        user_one = self.user("FD-1", "甲")
        with SessionLocal() as db:
            popular = Asset(object_key="yxb/popular.mp4", filename="popular.mp4", media_type="video", size=1)
            normal = Asset(object_key="yxb/normal.mp4", filename="normal.mp4", media_type="video", size=1)
            db.add_all([popular, normal])
            db.flush()
            db.add_all([
                AssetFavorite(user_number="FD-1", asset_id=popular.id),
                AssetFavorite(user_number="FD-2", asset_id=popular.id),
                AssetFavorite(user_number="FD-1", asset_id=normal.id),
            ])
            db.commit()
            page = list_assets(
                q="", category="", content_type="", status="", media_type="",
                asset_scope="", library_type="", asset_subtype="", ingest_source="",
                directory="", favorite=None, hot_only=False, sort="favorites",
                page=1, page_size=24, db=db, user=user_one,
            )
            self.assertEqual(page.items[0].id, popular.id)
            self.assertEqual(page.items[0].favorite_count, 2)
            self.assertTrue(page.items[0].favorite)
            self.assertEqual(page.items[1].favorite_count, 1)

    def test_channels_choice_interaction_is_owner_scoped_and_queued(self):
        service = ChannelsService()
        service._sessions["session"] = {
            "id": "session", "owner_number": "FD-1", "owner_name": "甲",
            "status": "waiting_choice", "message": "请选择", "qr": b"image",
            "account_id": "", "interaction_required": True, "capture_mode": "account_choice",
            "commands": [], "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
        }
        result = service.queue_interaction("session", "FD-1", 0.5, 0.25)
        self.assertEqual(result["status"], "selecting_account")
        self.assertEqual(service._sessions["session"]["commands"][0]["type"], "click")
        with self.assertRaises(ChannelsError):
            service.queue_interaction("session", "FD-2", 0.5, 0.25)

        service._sessions["scroll-session"] = {
            "id": "scroll-session", "owner_number": "FD-1", "owner_name": "甲",
            "status": "waiting_choice", "message": "请选择", "qr": b"image",
            "account_id": "", "interaction_required": True, "capture_mode": "account_choice",
            "commands": [], "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
        }
        scrolled = service.queue_interaction(
            "scroll-session", "FD-1", 0.79, 0.58, action="scroll", delta_y=720
        )
        self.assertEqual(scrolled["status"], "waiting_choice")
        self.assertTrue(scrolled["interaction_required"])
        self.assertEqual(service._sessions["scroll-session"]["commands"][0]["type"], "scroll")
        self.assertEqual(service._sessions["scroll-session"]["commands"][0]["delta_y"], 720)
        service.queue_interaction(
            "scroll-session", "FD-1", 0.79, 0.58, action="scroll_accounts", delta_y=720
        )
        self.assertEqual(len(service._sessions["scroll-session"]["commands"]), 1)
        self.assertEqual(service._sessions["scroll-session"]["commands"][0]["delta_y"], 1440)
        with self.assertRaises(ChannelsError):
            service.queue_interaction("scroll-session", "FD-1", 0.5, 0.5, action="drag")

        service._sessions["semantic-session"] = {
            "id": "semantic-session", "owner_number": "FD-1", "owner_name": "甲",
            "status": "waiting_choice", "message": "请选择", "qr": b"image",
            "account_id": "", "interaction_required": True, "capture_mode": "account_list",
            "account_choices": [{"choice_id": "choice-2", "label": "品牌号", "detail": "运营者"}],
            "account_choice_targets": {
                "choice-2": {"text": "品牌号 运营者", "frame_index": 0},
            },
            "commands": [], "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
        }
        selected = service.queue_interaction(
            "semantic-session", "FD-1", 0.5, 0.5,
            action="select_account", choice_id="choice-2",
        )
        self.assertEqual(selected["status"], "selecting_account")
        self.assertEqual(service._sessions["semantic-session"]["commands"][0]["type"], "select")
        self.assertEqual(service._sessions["semantic-session"]["commands"][0]["choice_id"], "choice-2")

    def test_channels_structured_choice_is_session_scoped_and_private(self):
        service = ChannelsService()
        now = datetime.utcnow()
        service._sessions["structured-session"] = {
            "id": "structured-session", "owner_number": "FD-1", "owner_name": "甲",
            "status": "waiting_choice", "message": "请选择", "qr": b"image",
            "account_id": "", "interaction_required": True, "capture_mode": "account_list",
            "account_choices": [{"choice_id": "choice-safe", "label": "品牌号", "detail": "运营者"}],
            "account_choice_targets": {
                "choice-safe": {"text": "品牌号 运营者", "frame_index": 1},
            },
            "commands": [], "created_at": now, "updated_at": now,
            "expires_at": now + timedelta(minutes=4),
        }

        public = service.session_status("structured-session", "FD-1")
        self.assertEqual(public["capture_mode"], "account_list")
        self.assertEqual(public["account_choices"], [
            {"choice_id": "choice-safe", "label": "品牌号", "detail": "运营者"},
        ])
        self.assertNotIn("account_choice_targets", public)
        self.assertNotIn("text", public["account_choices"][0])
        self.assertNotIn("frame_index", public["account_choices"][0])

        with self.assertRaisesRegex(ChannelsError, "候选项已失效"):
            service.queue_interaction(
                "structured-session", "FD-1", 0.5, 0.5,
                action="select", choice_id="forged-choice",
            )

        selected = service.queue_interaction(
            "structured-session", "FD-1", 0.5, 0.5,
            action="select", choice_id="choice-safe",
        )
        self.assertEqual(selected["status"], "selecting_account")
        command = service._sessions["structured-session"]["commands"][0]
        self.assertEqual(command["type"], "select")
        self.assertEqual(command["target"]["text"], "品牌号 运营者")
        self.assertEqual(selected["account_choices"], [])

    def test_channels_choice_discovery_combines_frames_and_preserves_same_name(self):
        class FakeFrame:
            def __init__(self, rows):
                self.rows = rows

            def evaluate(self, _script):
                return self.rows

        page = MagicMock()
        page.frames = [
            FakeFrame([]),
            FakeFrame([
                {"text": "WIS品牌号 运营者", "label": "WIS品牌号", "detail": "运营者"},
                {"text": "WIS品牌号 管理员", "label": "WIS品牌号", "detail": "管理员"},
                {"text": "WIS品牌号 运营者", "label": "WIS品牌号", "detail": "运营者"},
            ]),
        ]

        public, private = ChannelsService._account_choice_items(page)
        self.assertEqual(len(public), 2)
        self.assertEqual([item["label"] for item in public], ["WIS品牌号", "WIS品牌号"])
        self.assertEqual({item["detail"] for item in public}, {"运营者", "管理员"})
        self.assertTrue(all(len(item["choice_id"]) == 24 for item in public))
        self.assertTrue(all(set(item) == {"choice_id", "label", "detail"} for item in public))
        self.assertEqual(len(private), 2)

    def test_channels_choice_click_relocates_exact_private_target(self):
        marker_locator = MagicMock()
        marker_locator.count.return_value = 1
        frame = MagicMock()
        frame.evaluate.return_value = {"matched": 1}
        frame.locator.return_value = marker_locator
        page = MagicMock()
        page.frames = [MagicMock(), frame]

        clicked, code = ChannelsService._click_account_choice(
            page, {"text": "品牌号 运营者", "frame_index": 1},
        )
        self.assertTrue(clicked)
        self.assertEqual(code, "")
        marker_locator.click.assert_called_once()

        changed, changed_code = ChannelsService._click_account_choice(
            page, {"text": "品牌号 运营者", "frame_index": 9},
        )
        self.assertFalse(changed)
        self.assertEqual(changed_code, "ACCOUNT_CHOICE_CHANGED")

    def test_channels_readback_confirms_submission_and_preserves_unknown_metrics(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/readback.mp4", filename="readback.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="readback-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/readback.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="readback-task", batch_id="readback-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="回流测试", status="submitted",
                failure_stage="confirm_publish", view_count=88, like_count=9,
            )
            db.add(task)
            db.flush()
            _apply_channels_readback(db, task, {
                "found": True, "platform_content_id": "video-100", "view_count": None,
                "like_count": 12, "comment_count": None,
                "published": True, "publication_state": "published",
                "message": "已在视频号主页回读确认",
            }, "2026-08-12")
            db.commit()
            self.assertEqual(task.status, "success")
            self.assertEqual(task.platform_content_id, "video-100")
            self.assertEqual(task.view_count, 88)
            self.assertEqual(task.like_count, 12)
            self.assertIsNone(task.comment_count)
            daily = db.scalar(select(ChannelsMetricDaily).where(ChannelsMetricDaily.delivery_id == task.id))
            self.assertIsNotNone(daily)
            self.assertEqual(daily.view_count, 88)
            self.assertIsNone(daily.comment_count)

    def test_channels_readback_does_not_call_listed_or_pending_content_public(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/readback-pending.mp4", filename="pending.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="readback-pending-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/pending.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="readback-pending-task", batch_id="readback-pending-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="待公开测试", status="submitted",
                failure_stage="confirm_publish",
            )
            db.add(task)
            db.flush()
            _apply_channels_readback(db, task, {
                "found": True, "published": False, "publication_state": "pending",
                "message": "视频号内容列表已受理，仍在等待平台审核",
            }, "2026-08-12")
            db.commit()
            self.assertEqual(task.status, "submitted")
            self.assertEqual(task.failure_stage, "confirm_publish")
            self.assertIsNone(task.published_at)
            self.assertIn("等待平台审核", task.message)

    def test_channels_missing_readback_does_not_stamp_a_fake_metrics_date(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/readback-missing.mp4", filename="missing.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="readback-missing-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/missing.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="readback-missing-task", batch_id="readback-missing-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="未找到测试", status="submitted",
                failure_stage="confirm_publish", metrics_date="2026-08-19",
            )
            db.add(task)
            db.flush()
            _apply_channels_readback(db, task, {
                "found": False,
                "message": "平台列表暂未找到该视频，稍后继续核验",
            }, "2026-08-20")
            db.commit()
            self.assertEqual(task.metrics_date, "")
            daily = db.scalar(select(ChannelsMetricDaily).where(ChannelsMetricDaily.delivery_id == task.id))
            self.assertIsNone(daily)

    def test_channels_partial_daily_readback_retries_before_marking_date_complete(self):
        meta_keys = {
            "channels.metrics.daily.last_completed_date",
            "channels.metrics.daily.state",
            "channels.metrics.daily.last_attempt_at",
            "channels.metrics.daily.completed_at",
            "channels.metrics.daily.processed",
            "channels.metrics.daily.errors",
        }
        with SessionLocal() as db:
            db.query(AppMeta).filter(AppMeta.key.in_(meta_keys)).delete(synchronize_session=False)
            db.commit()

        first_now = datetime(2026, 8, 20, 1, 0, 0)
        with patch("app.main._readback_channels_tasks", return_value={"processed": 0, "errors": 1}) as readback:
            first = run_channels_daily_metrics_if_due(first_now)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["errors"], 1)
        readback.assert_called_once()
        with SessionLocal() as db:
            self.assertEqual(db.get(AppMeta, "channels.metrics.daily.state").value, "partial")
            self.assertIsNone(db.get(AppMeta, "channels.metrics.daily.last_completed_date"))

        with patch("app.main._readback_channels_tasks") as readback:
            waiting = run_channels_daily_metrics_if_due(datetime(2026, 8, 20, 1, 10, 0))
        self.assertEqual(waiting["status"], "waiting")
        readback.assert_not_called()

        with patch("app.main._readback_channels_tasks", return_value={"processed": 3, "errors": 0}) as readback:
            recovered = run_channels_daily_metrics_if_due(datetime(2026, 8, 20, 1, 31, 0))
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["errors"], 0)
        readback.assert_called_once()
        with SessionLocal() as db:
            self.assertEqual(db.get(AppMeta, "channels.metrics.daily.state").value, "completed")
            self.assertEqual(db.get(AppMeta, "channels.metrics.daily.last_completed_date").value, "2026-08-19")
            db.query(AppMeta).filter(AppMeta.key.in_(meta_keys)).delete(synchronize_session=False)
            db.commit()

    def test_channels_missing_readback_clears_legacy_date_for_success_without_metrics(self):
        with SessionLocal() as db:
            account = ChannelsAccount(
                id="readback-success-missing-account", owner_number="FD-1", owner_name="甲",
                nickname="成功记录视频号", auth_file="/data/channels-auth/success-missing.json", status="active",
            )
            asset = Asset(
                id=991014, object_key="yxb/success-missing.mp4", filename="success-missing.mp4",
                media_type="video", size=1024,
            )
            task = ChannelsDelivery(
                id="readback-success-missing-task", batch_id="readback-success-missing-batch",
                asset_id=asset.id, created_by_number="FD-1", created_by_name="甲",
                account_id=account.id, account_name=account.nickname, title="成功但未回流测试",
                status="success", metrics_date="2026-08-19",
            )
            db.add_all([account, asset, task])
            db.flush()
            _apply_channels_readback(db, task, {
                "found": False,
                "message": "平台列表暂未找到该视频，稍后继续核验",
            }, "2026-08-20")
            db.commit()
            self.assertEqual(task.metrics_date, "")
            self.assertIsNone(db.scalar(select(ChannelsMetricDaily).where(
                ChannelsMetricDaily.delivery_id == task.id,
            )))

    def test_channels_expired_authorization_pauses_readback_without_fake_date(self):
        with SessionLocal() as db:
            account = ChannelsAccount(
                id="paused-expired", owner_number="FD-1", owner_name="甲", nickname="待重扫视频号",
                auth_file="/data/channels-auth/paused.json", status="expired",
            )
            asset = Asset(
                id=991012, object_key="yxb/paused.mp4", filename="paused.mp4", media_type="video", size=1024,
            )
            task = ChannelsDelivery(
                id="paused-task", batch_id="paused-batch", asset_id=asset.id,
                account_id=account.id, account_name=account.nickname,
                created_by_number="FD-1", created_by_name="甲", title="待重扫任务",
                status="submitted", failure_stage="confirm_publish", submitted_at=datetime.utcnow(),
                metrics_date="2026-08-19",
            )
            db.add_all([account, asset, task])
            db.commit()

        result = _readback_channels_tasks(["paused-task"], "2026-08-20")

        self.assertEqual(result, {"processed": 0, "errors": 1})
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "paused-task")
            self.assertEqual(task.failure_stage, "authorization")
            self.assertEqual(task.metrics_date, "")
            self.assertIn("重新扫码", task.metrics_message)

    def test_channels_publish_acceptance_stays_submitted_until_public_readback(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/accepted.mp4", filename="accepted.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="accepted-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/accepted.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="accepted-task", batch_id="accepted-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="受理待确认", status="pending",
            )
            db.add(task)
            db.commit()
        with patch("app.main.channels_service.publish", return_value={
            "accepted": True,
            "confirmed": False,
            "platform_content_id": "upload-only-id",
            "message": "视频号平台已受理，正在核验内容列表与公开展示状态",
        }) as publish:
            run_channels_push("accepted-task")
        self.assertEqual(publish.call_args.kwargs["video_annotation"], "none")
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "accepted-task")
            self.assertEqual(task.status, "submitted")
            self.assertEqual(task.failure_stage, "confirm_publish")
            self.assertIsNotNone(task.submitted_at)
            self.assertIsNone(task.published_at)

    def test_channels_persists_publish_boundary_before_uncertain_response(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/checkpoint.mp4", filename="checkpoint.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="checkpoint-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/checkpoint.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add(ChannelsDelivery(
                id="checkpoint-task", batch_id="checkpoint-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="防重发检查点", status="pending",
            ))
            db.commit()

        def publish_then_disconnect(**kwargs):
            kwargs["mark_submitted"]("client-checkpoint-1")
            raise ChannelsError("最终提交后连接中断", "confirm_publish")

        with patch("app.main.channels_service.publish", side_effect=publish_then_disconnect):
            run_channels_push("checkpoint-task")

        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "checkpoint-task")
            self.assertEqual(task.status, "submitted")
            self.assertEqual(task.failure_stage, "confirm_publish")
            self.assertIsNotNone(task.publish_clicked_at)
            self.assertEqual(task.publish_client_id, "client-checkpoint-1")
            self.assertIsNotNone(task.submitted_at)
            # Simulate a stale worker row restored by a process restart.
            task.status = "publishing"
            db.commit()

        with patch("app.main.channels_service.publish") as publish:
            run_channels_push("checkpoint-task")
        publish.assert_not_called()
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "checkpoint-task")
            self.assertEqual(task.status, "submitted")
            self.assertIn("锁定重复上传", task.message)

    def test_channels_page_success_stays_submitted_until_public_readback(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/published.mp4", filename="published.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="published-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/published.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add(ChannelsDelivery(
                id="published-task", batch_id="published-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="明确成功", status="pending",
                cover_object_key="yxb/cover.jpg", cover_filename="cover.jpg",
            ))
            db.commit()
        with patch("app.main.channels_service.publish", return_value={
            "accepted": True,
            "confirmed": True,
            "cover_warning": "平台自定义封面入口暂不可用",
            "message": "视频号平台已明确返回发布成功；自定义封面待后续核验",
        }):
            run_channels_push("published-task")
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "published-task")
            self.assertEqual(task.status, "submitted")
            self.assertEqual(task.failure_stage, "confirm_publish")
            self.assertIsNone(task.published_at)

    def test_channels_ambiguous_disconnect_after_submit_never_reuploads_directly(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/ambiguous.mp4", filename="ambiguous.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="ambiguous-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/ambiguous.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add(ChannelsDelivery(
                id="ambiguous-task", batch_id="ambiguous-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="回执中断", status="pending",
            ))
            db.commit()
        with patch(
            "app.main.channels_service.publish",
            side_effect=ChannelsError("等待发布回执阶段网络连接中断", "confirm_publish"),
        ):
            run_channels_push("ambiguous-task")
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, "ambiguous-task")
            self.assertEqual(task.status, "submitted")
            self.assertEqual(task.failure_stage, "confirm_publish")
            self.assertEqual(task.error_message, "")
            self.assertIsNotNone(task.submitted_at)
            self.assertIn("不会直接重复上传", task.message)

    def test_channels_capture_revision_changes_only_for_new_pixels(self):
        service = ChannelsService()
        service._sessions["capture-session"] = {
            "id": "capture-session", "owner_number": "FD-1", "owner_name": "甲",
            "status": "waiting_scan", "message": "请扫码", "qr": b"",
            "capture_hash": "", "capture_revision": 0,
            "account_id": "", "interaction_required": False, "capture_mode": "qr",
            "commands": [], "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
        }
        self.assertTrue(service._update_capture("capture-session", b"first-image", capture_mode="qr"))
        first = service.session_status("capture-session", "FD-1")
        self.assertEqual(first["capture_revision"], 1)
        self.assertFalse(service._update_capture("capture-session", b"first-image", capture_mode="qr"))
        self.assertEqual(service.session_status("capture-session", "FD-1")["capture_revision"], 1)
        self.assertTrue(service._update_capture("capture-session", b"second-image", capture_mode="account_choice"))
        self.assertEqual(service.session_status("capture-session", "FD-1")["capture_revision"], 2)

    def test_channels_account_scroll_prefers_inner_scroll_container(self):
        page = MagicMock()
        page.evaluate.return_value = {"moved": True, "before": 0, "after": 420}
        self.assertTrue(ChannelsService._scroll_account_choice(page, 100, 200, 720))
        page.mouse.wheel.assert_not_called()

        page.evaluate.return_value = {"moved": False}
        self.assertFalse(ChannelsService._scroll_account_choice(page, 100, 200, -720))
        page.mouse.move.assert_called_with(100, 200)
        page.mouse.wheel.assert_called_with(0, -720)

    def test_channels_authorization_expiry_detects_url_and_visible_login_copy(self):
        page = MagicMock()
        page.url = "https://channels.weixin.qq.com/login.html"
        page.frames = []
        self.assertTrue(ChannelsService._authorization_expired(page))

        page.url = "https://channels.weixin.qq.com/platform/post/create"
        marker = MagicMock()
        marker.count.return_value = 1
        marker.is_visible.return_value = True
        page.get_by_text.return_value.first = marker
        self.assertTrue(ChannelsService._authorization_expired(page))

        marker.is_visible.return_value = False
        self.assertFalse(ChannelsService._authorization_expired(page))

        login_frame = MagicMock()
        login_frame.url = "https://open.weixin.qq.com/connect/qrconnect?appid=test"
        page.frames = [login_frame]
        page.main_frame = MagicMock()
        self.assertTrue(ChannelsService._authorization_expired(page))

    def test_channels_fresh_authorization_rebinds_and_resumes_expired_tasks(self):
        with SessionLocal() as db:
            expired = ChannelsAccount(
                id="resume-expired", owner_number="FD-1", owner_name="甲", nickname="同一视频号",
                auth_file="/data/channels-auth/expired.json", status="expired",
            )
            fresh = ChannelsAccount(
                id="resume-fresh", owner_number="FD-1", owner_name="甲", nickname="同一视频号",
                auth_file="/data/channels-auth/fresh.json", status="active",
            )
            asset = Asset(
                id=991010, object_key="yxb/resume.mp4", filename="resume.mp4", media_type="video", size=1024,
            )
            failed = ChannelsDelivery(
                id="resume-failed", batch_id="resume-batch", asset_id=asset.id,
                account_id=expired.id, account_name=expired.nickname,
                created_by_number="FD-1", created_by_name="甲", title="授权失败任务",
                status="failed", failure_stage="open_publish_page",
                message="视频号授权已过期，请重新扫码授权", error_message="视频号授权已过期，请重新扫码授权",
            )
            submitted = ChannelsDelivery(
                id="resume-submitted", batch_id="resume-batch", asset_id=asset.id,
                account_id=expired.id, account_name=expired.nickname,
                created_by_number="FD-1", created_by_name="甲", title="待公开任务",
                status="submitted", failure_stage="authorization", metrics_message="授权已失效",
                submitted_at=datetime.utcnow(),
            )
            db.add_all([expired, fresh, asset, failed, submitted])
            db.flush()

            resumed = ChannelsService._resume_tasks_after_authorization(db, fresh)
            db.commit()

            self.assertEqual(resumed, 2)
            self.assertEqual(failed.account_id, fresh.id)
            self.assertEqual(failed.status, "pending")
            self.assertEqual(failed.error_message, "")
            self.assertEqual(submitted.account_id, fresh.id)
            self.assertEqual(submitted.failure_stage, "confirm_publish")
            self.assertIn("等待继续回流", submitted.metrics_message)

    def test_channels_readback_rebinds_expired_task_to_existing_active_copy(self):
        with SessionLocal() as db:
            expired = ChannelsAccount(
                id="resolver-expired", owner_number="FD-1", owner_name="甲", nickname="同一视频号",
                auth_file="/data/channels-auth/expired.json", status="expired",
            )
            active = ChannelsAccount(
                id="resolver-active", owner_number="FD-1", owner_name="甲", nickname="同一视频号",
                auth_file="/data/channels-auth/active.json", status="active", authorized_at=datetime.utcnow(),
            )
            asset = Asset(
                id=991011, object_key="yxb/resolver.mp4", filename="resolver.mp4", media_type="video", size=1024,
            )
            task = ChannelsDelivery(
                id="resolver-task", batch_id="resolver-batch", asset_id=asset.id,
                account_id=expired.id, account_name=expired.nickname,
                created_by_number="FD-1", created_by_name="甲", title="待公开任务",
                status="submitted", failure_stage="authorization", submitted_at=datetime.utcnow(),
            )
            db.add_all([expired, active, asset, task])
            db.flush()

            resolved = _active_channels_account_for_task(db, task)

            self.assertEqual(resolved.id, active.id)
            self.assertEqual(task.account_id, active.id)
            self.assertEqual(task.failure_stage, "confirm_publish")

    def test_channels_retry_expired_authorization_does_not_requeue_same_account(self):
        with SessionLocal() as db:
            account = ChannelsAccount(
                id="expired-account",
                owner_number="FD-1",
                owner_name="甲",
                nickname="过期视频号",
                auth_file="/data/channels-auth/expired.json",
                status="active",
                authorized_at=datetime.utcnow(),
            )
            asset = Asset(
                id=991001,
                object_key="yxb/expired-retry.mp4",
                filename="expired-retry.mp4",
                media_type="video",
                size=1024,
            )
            task = ChannelsDelivery(
                id="expired-retry-task",
                batch_id="expired-retry-batch",
                asset_id=asset.id,
                account_id=account.id,
                account_name=account.nickname,
                created_by_number="FD-1",
                created_by_name="甲",
                title="过期重试",
                status="failed",
                failure_stage="open_publish_page",
                message="视频号授权已过期，请重新扫码授权",
                error_message="视频号授权已过期，请重新扫码授权",
            )
            db.add_all([account, asset, task])
            db.commit()

            with self.assertRaises(HTTPException) as raised:
                channels_task_retry(task.id, db, {"number": "FD-1", "realName": "甲"})

            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("重新扫码", str(raised.exception.detail))
            db.refresh(account)
            db.refresh(task)
            self.assertEqual(account.status, "expired")
            self.assertEqual(task.status, "failed")

    def test_channels_retry_never_reuploads_task_with_platform_receipt_evidence(self):
        with SessionLocal() as db:
            account = ChannelsAccount(
                id="receipt-account", owner_number="FD-1", owner_name="甲",
                nickname="回执视频号", auth_file="/data/channels-auth/receipt.json", status="active",
            )
            asset = Asset(object_key="yxb/receipt.mp4", filename="receipt.mp4", media_type="video", size=1024)
            db.add_all([account, asset])
            db.flush()
            task = ChannelsDelivery(
                id="receipt-task", batch_id="receipt-batch", asset_id=asset.id,
                account_id=account.id, account_name=account.nickname,
                created_by_number="FD-1", created_by_name="甲", title="回执待核验",
                status="failed", failure_stage="confirm_publish",
                platform_export_id="export/receipt", submitted_at=datetime.utcnow(),
            )
            db.add(task)
            db.commit()

            with patch("app.main.channels_service.read_delivery", return_value={
                "found": False, "message": "内容列表暂未找到",
            }):
                result = channels_task_retry(task.id, db, {"number": "FD-1", "realName": "甲"})

            db.refresh(task)
            self.assertEqual(result["status"], "submitted")
            self.assertEqual(task.status, "submitted")
            self.assertEqual(task.failure_stage, "confirm_publish")
            self.assertIn("锁定重复上传", task.message)

    def test_channels_product_row_parser_keeps_real_id_name_and_price(self):
        parsed = ChannelsService._parse_product_row(
            "【官方正品】WIS隐形水润保湿舒缓修护面膜\nID\n10001206069243\n自营\n¥108\n9961216",
            "https://example.test/product.jpg",
        )
        self.assertEqual(parsed["id"], "10001206069243")
        self.assertEqual(parsed["name"], "【官方正品】WIS隐形水润保湿舒缓修护面膜")
        self.assertEqual(parsed["price_yuan"], 108.0)
        self.assertEqual(parsed["image_url"], "https://example.test/product.jpg")

    def test_channels_product_identity_matches_id_or_product_name_without_false_zero(self):
        self.assertTrue(ChannelsService._product_identity_matches(
            "已关联商品 ID：10001206069243", "10001206069243", "WIS隐形水润面膜"
        ))
        self.assertTrue(ChannelsService._product_identity_matches(
            "【官方正品】WIS隐形水润面膜", "", "WIS 隐形水润面膜"
        ))
        self.assertFalse(ChannelsService._product_identity_matches(
            "其他商品 10001206069244", "10001206069243", "WIS隐形水润面膜"
        ))

    def test_channels_window_product_parser_uses_platform_id_price_and_image(self):
        parsed = ChannelsService._window_product_to_item({
            "productId": "10001206069243",
            "title": "WIS隐形水润面膜",
            "sellingPrice": 10800,
            "imgUrls": ["https://example.test/wis.jpg"],
        })
        self.assertEqual(parsed["id"], "10001206069243")
        self.assertEqual(parsed["name"], "WIS隐形水润面膜")
        self.assertEqual(parsed["price_yuan"], 108.0)
        self.assertEqual(parsed["image_url"], "https://example.test/wis.jpg")

    def test_channels_product_status_returns_cached_items_immediately_and_filters_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChannelsService()
            service.auth_root = Path(directory)
            auth_file = Path(directory) / "account.json"
            auth_file.write_text("{}", encoding="utf-8")
            account = ChannelsAccount(id="account", auth_file=str(auth_file), status="active")
            service._product_cache[account.id] = {
                "items": [
                    {"id": "10001", "name": "WIS隐形水润面膜", "price_yuan": 108, "image_url": ""},
                    {"id": "10002", "name": "WIS晶润眼膜", "price_yuan": 99, "image_url": ""},
                ],
                "fetched_at": time.time(),
                "read_at": "2026-08-12T08:00:00Z",
                "complete": True,
                "expected_total": 2,
                "page_count": 1,
            }
            result = service.products_status(account, "水润")
            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["items"][0]["id"], "10001")
            self.assertEqual(result["cached_total"], 2)
            self.assertEqual(result["retry_after_ms"], 0)
            self.assertTrue(result["complete"])

    def test_channels_legacy_partial_product_cache_refreshes_without_hiding_cached_items(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChannelsService()
            service.auth_root = Path(directory)
            auth_file = Path(directory) / "account.json"
            auth_file.write_text("{}", encoding="utf-8")
            account = ChannelsAccount(id="account", auth_file=str(auth_file), status="active")
            service._product_cache[account.id] = {
                "items": [{"id": "10001", "name": "历史首屏商品", "price_yuan": 108, "image_url": ""}],
                "fetched_at": time.time(),
                "read_at": "2026-08-12T08:00:00Z",
            }
            with patch("app.channels_service.threading.Thread") as thread_class:
                result = service.products_status(account)
            self.assertEqual(result["state"], "refreshing")
            self.assertEqual(result["cached_total"], 1)
            self.assertFalse(result["complete"])
            self.assertIn("补齐完整列表", result["message"])
            thread_class.return_value.start.assert_called_once()

    def test_channels_product_status_starts_only_one_background_job_for_cold_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ChannelsService()
            service.auth_root = Path(directory)
            auth_file = Path(directory) / "account.json"
            auth_file.write_text("{}", encoding="utf-8")
            account = ChannelsAccount(id="account", auth_file=str(auth_file), status="active")
            with patch("app.channels_service.threading.Thread") as thread_class:
                first = service.products_status(account)
                second = service.products_status(account)
            self.assertEqual(first["state"], "loading")
            self.assertEqual(second["state"], "loading")
            self.assertEqual(thread_class.call_count, 1)
            thread_class.return_value.start.assert_called_once()

    def test_channels_product_errors_are_translated_for_colleagues(self):
        message, stage = ChannelsService._friendly_product_error(
            RuntimeError("Locator.wait_for: Timeout 15000ms exceeded")
        )
        self.assertIn("页面响应较慢", message)
        self.assertEqual(stage, "product_read")
        message, stage = ChannelsService._friendly_product_error(
            ChannelsError("视频号授权已过期", "product_dialog")
        )
        self.assertIn("重新扫码授权", message)
        self.assertEqual(stage, "authorization")

    def test_channels_errors_keep_the_exact_failed_stage(self):
        error = ChannelsError("等待平台处理视频阶段响应超时", "wait_platform")
        self.assertEqual(str(error), "等待平台处理视频阶段响应超时")
        self.assertEqual(error.stage, "wait_platform")

    def test_channels_button_disabled_css_class_is_not_treated_as_clickable(self):
        class FakeButton:
            def __init__(self, class_name="", aria_disabled="", native_disabled=False):
                self.class_name = class_name
                self.aria_disabled = aria_disabled
                self.native_disabled = native_disabled

            def is_disabled(self):
                return self.native_disabled

            def get_attribute(self, name):
                return {"class": self.class_name, "aria-disabled": self.aria_disabled}.get(name)

        self.assertTrue(ChannelsService._button_visually_disabled(FakeButton("weui-desktop-btn_disabled")))
        self.assertTrue(ChannelsService._button_visually_disabled(FakeButton(aria_disabled="true")))
        self.assertTrue(ChannelsService._button_visually_disabled(FakeButton(native_disabled=True)))
        self.assertFalse(ChannelsService._button_visually_disabled(FakeButton("weui-desktop-btn_primary")))

    def test_channels_short_title_is_padded_to_platform_minimum(self):
        self.assertEqual(ChannelsService._normalized_short_title("测试", "测试2.mp4"), "测试视频素材")
        self.assertEqual(ChannelsService._normalized_short_title("", "测试2.mp4"), "测试2视频素材")
        self.assertEqual(len(ChannelsService._normalized_short_title("这是一个超过十六个字的超长视频标题必须截断", "a.mp4")), 16)

    def test_channels_readback_searches_the_platform_short_title_first(self):
        candidates = ChannelsService._delivery_search_candidates(
            "8.19-次抛-精-心如车次抛七夕-dx",
            "8.19-次抛-精-心如车次抛七夕-dx.mp4",
        )
        self.assertEqual(candidates[0], "8.19-次抛-精-心如车次抛七")
        self.assertIn("8.19-次抛-精-心如车次抛七夕-dx"[:32], candidates)

    def test_channels_short_title_is_written_and_read_back(self):
        page = MagicMock()
        publish_root = MagicMock()
        field = MagicMock()
        field.input_value.return_value = "这是明确填写的短标题"
        with patch.object(ChannelsService, "_first_visible", return_value=field):
            result = ChannelsService._fill_short_title(
                page, publish_root, "这是明确填写的短标题", "原始文件名.mp4",
            )
        self.assertEqual(result, "这是明确填写的短标题")
        field.fill.assert_called_once_with("这是明确填写的短标题", timeout=10000)

    def test_channels_api_record_requires_real_identity_evidence(self):
        unrelated = {"exportId": "export/unrelated", "description": "同批次另一条视频"}
        self.assertIsNone(ChannelsService._matching_post_api_record(
            [unrelated], ["本条明确短标题"], "",
        ))
        matched = {"exportId": "export/matched", "title": "本条明确短标题"}
        self.assertIs(ChannelsService._matching_post_api_record(
            [unrelated, matched], ["本条明确短标题"], "",
        ), matched)
        self.assertIsNone(ChannelsService._matching_post_api_record(
            [matched], ["本条明确短标题"], "", excluded_platform_ids={"export/matched"},
        ))

    def test_channels_publish_ack_requires_semantic_publish_endpoint(self):
        accepted = ChannelsService._publish_acknowledgement(
            "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post_create",
            200,
            {"errCode": 0, "data": {"exportId": "export/accepted"}},
        )
        self.assertEqual(accepted["platform_export_id"], "export/accepted")
        self.assertIsNone(ChannelsService._publish_acknowledgement(
            "https://channels.weixin.qq.com/platform/post/list", 200, {"errCode": 0},
        ))
        self.assertIsNone(ChannelsService._publish_acknowledgement(
            "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post_create",
            200,
            {"errCode": 300002, "errMsg": "rejected"},
        ))
        self.assertIsNone(ChannelsService._publish_acknowledgement(
            "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post_create", 200, {"data": {}},
        ))

    def test_channels_export_identity_matches_full_or_suffix_form(self):
        record = {"exportId": "export/UzFf-test", "description": "无标题字段"}
        self.assertIs(ChannelsService._matching_post_api_record(
            [record], ["不存在的标题"], "UzFf-test",
        ), record)
        self.assertIs(ChannelsService._matching_post_api_record(
            [record], ["不存在的标题"], "", "export/UzFf-test",
        ), record)

    def test_channels_publication_state_separates_listed_pending_and_public(self):
        self.assertEqual(ChannelsService._publication_state("审核中 请耐心等待"), "pending")
        self.assertEqual(ChannelsService._publication_state("审核失败 内容违规"), "failed")
        self.assertEqual(ChannelsService._publication_state("仅自己可见"), "private")
        self.assertEqual(ChannelsService._publication_state("已发布 播放 18"), "published")
        self.assertEqual(ChannelsService._publication_state("内容列表里的一条记录"), "listed")

    def test_channels_creator_api_collects_http_201_nested_title_and_ids(self):
        record = {"objectId": "export/real-post", "exportId": "export/real-post",
                  "status": 1, "visibleType": 1,
                  "desc": {"shortTitle": [{"shortTitle": "精确独立短标题"}], "description": "不同的正文"}}
        response = MagicMock(status=201, url="https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/post_list",
                             headers={"content-type": "application/json"})
        response.json.return_value = {"data": {"list": [record]}}
        records = []
        ChannelsService._collect_post_api_records(response, records)
        self.assertIs(ChannelsService._matching_post_api_record(records, ["精确独立短标题"], ""), record)
        response.status = 302
        refused = []
        ChannelsService._collect_post_api_records(response, refused)
        self.assertEqual(refused, [])

    def test_channels_api_publication_requires_explicit_normal_and_public(self):
        self.assertEqual(ChannelsService._api_publication_state({"status": 1, "visibleType": 1}), "published")
        for record in [None, {}, {"status": 1}, {"visibleType": 1}, {"status": 0, "visibleType": 1},
                       {"status": 1, "visibleType": 2}, {"status": True, "visibleType": True}]:
            self.assertEqual(ChannelsService._api_publication_state(record), "listed")

    def test_channels_creator_list_navigation_recovers_home_redirect(self):
        page = MagicMock()
        content, video, marker = MagicMock(), MagicMock(), MagicMock()
        with patch.object(ChannelsService, '_goto_creator_page') as navigate, \
             patch.object(ChannelsService, '_first_visible', side_effect=[None, content, video, marker]):
            ChannelsService._goto_creator_post_list(page)
        navigate.assert_called_once()
        content.click.assert_called_once()
        video.click.assert_called_once()

    def test_channels_creator_homepage_is_not_an_empty_publication_list(self):
        with patch.object(ChannelsService, '_goto_creator_page'), \
             patch.object(ChannelsService, '_first_visible', return_value=None):
            with self.assertRaisesRegex(Exception, '作品列表尚未加载'):
                ChannelsService._goto_creator_post_list(MagicMock())

    def test_channels_oss_download_resumes_after_interruption(self):
        class FakeResponse:
            def __init__(self, status_code, headers, chunks, failure=None):
                self.status_code = status_code
                self.headers = headers
                self.chunks = chunks
                self.failure = failure

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                for chunk in self.chunks:
                    yield chunk
                if self.failure:
                    raise self.failure

        first = FakeResponse(200, {"Content-Length": "6"}, [b"abc"], RuntimeError("connection reset"))
        second = FakeResponse(206, {"Content-Length": "3", "Content-Range": "bytes 3-5/6"}, [b"def"])
        reports = []
        with tempfile.TemporaryDirectory() as folder, \
            patch("app.channels_service.requests.get", side_effect=[first, second]) as request, \
            patch("app.channels_service.time.sleep"):
            destination = Path(folder) / "video.mp4"
            ChannelsService._download_with_resume(
                "https://example.test/video.mp4",
                destination,
                "download_original",
                lambda stage, message: reports.append((stage, message)),
                "素材库原视频",
            )
            self.assertEqual(destination.read_bytes(), b"abcdef")
        self.assertNotIn("Range", request.call_args_list[0].kwargs["headers"])
        self.assertEqual(request.call_args_list[1].kwargs["headers"]["Range"], "bytes=3-")
        self.assertTrue(any("续传" in message for _stage, message in reports))

    def test_channels_transient_upload_errors_are_retryable(self):
        self.assertTrue(ChannelsService._transient_upload_error("网络出错，请重新上传"))
        self.assertTrue(ChannelsService._transient_upload_error("Connection reset by peer"))
        self.assertFalse(ChannelsService._transient_upload_error("格式不支持"))

    def test_channels_short_title_uses_native_setter_after_locator_timeout(self):
        page = MagicMock()
        publish_root = MagicMock()
        field = MagicMock()
        field.fill.side_effect = RuntimeError("overlay intercepts pointer events")
        field.input_value.return_value = "六字标题视频素材"
        with (
            patch.object(ChannelsService, "_dismiss_non_publish_dialogs"),
            patch.object(ChannelsService, "_first_visible", return_value=field),
        ):
            result = ChannelsService._fill_short_title(page, publish_root, "六字标题", "fallback.mp4")
        self.assertEqual(result, "六字标题视频素材")
        field.evaluate.assert_called_once()

    def test_channels_custom_cover_finds_dialog_input_after_opening_new_cover_ui(self):
        page = MagicMock()
        publish_root = MagicMock()
        trigger = MagicMock()
        image_input = MagicMock()
        confirm = MagicMock()
        cover_path = Path("cover-9x16.jpg")
        confirm.inner_text.return_value = "确定"
        with (
            patch.object(ChannelsService, "_wait_cover_image_input", side_effect=[None, image_input]),
            patch.object(ChannelsService, "_find_cover_trigger", return_value=trigger) as find_trigger,
            patch.object(ChannelsService, "_click_cover_trigger", return_value=False) as click_trigger,
            patch.object(ChannelsService, "_cover_roots", return_value=[publish_root, page]),
            patch.object(ChannelsService, "_first_visible", side_effect=[confirm, None]),
        ):
            ChannelsService._apply_custom_cover(page, publish_root, cover_path)

        self.assertIn("视频封面", find_trigger.call_args.args[2].pattern)
        click_trigger.assert_called_once_with(page, trigger, cover_path)
        image_input.set_input_files.assert_called_once_with(str(cover_path))
        confirm.click.assert_called_once()
        page.wait_for_timeout.assert_called_with(1200)

    def test_channels_custom_cover_accepts_new_auto_apply_input_without_confirm_button(self):
        page = MagicMock()
        publish_root = MagicMock()
        image_input = MagicMock()
        with (
            patch.object(ChannelsService, "_wait_cover_image_input", return_value=image_input),
            patch.object(ChannelsService, "_cover_roots", return_value=[publish_root, page]),
            patch.object(ChannelsService, "_first_visible", return_value=None),
        ):
            ChannelsService._apply_custom_cover(page, publish_root, Path("cover-9x16.jpg"))
        image_input.set_input_files.assert_called_once()
        page.wait_for_timeout.assert_any_call(1800)

    def test_channels_custom_cover_accepts_auto_apply_after_platform_clears_file_input(self):
        page = MagicMock()
        publish_root = MagicMock()
        image_input = MagicMock()
        with (
            patch.object(ChannelsService, "_wait_cover_image_input", return_value=image_input),
            patch.object(ChannelsService, "_cover_roots", return_value=[publish_root, page]),
            patch.object(ChannelsService, "_first_visible", return_value=None),
        ):
            ChannelsService._apply_custom_cover(page, publish_root, Path("cover-9x16.jpg"))
        image_input.set_input_files.assert_called_once()

    def test_channels_custom_cover_retries_platform_network_error_before_publish(self):
        page = MagicMock()
        publish_root = MagicMock()
        transient = ChannelsError("视频号未接受自定义封面：网络出错，请重新上传。", "set_cover")
        with (
            patch.object(ChannelsService, "_apply_custom_cover_once", side_effect=[transient, None]) as apply_once,
            patch.object(ChannelsService, "_dismiss_cover_retry_notice") as dismiss,
        ):
            ChannelsService._apply_custom_cover(page, publish_root, Path("cover-9x16.jpg"))
        self.assertEqual(apply_once.call_count, 2)
        dismiss.assert_called_once()
        page.wait_for_timeout.assert_called_once_with(1200)

    def test_channels_custom_cover_stops_when_browser_cannot_assign_file(self):
        page = MagicMock()
        publish_root = MagicMock()
        image_input = MagicMock()
        image_input.set_input_files.side_effect = RuntimeError("input detached")
        with (
            patch.object(ChannelsService, "_wait_cover_image_input", return_value=image_input),
            self.assertRaises(ChannelsError) as raised,
        ):
            ChannelsService._apply_custom_cover(page, publish_root, Path("cover-9x16.jpg"))
        self.assertEqual(raised.exception.stage, "set_cover")
        self.assertIn("封面文件未能交给视频号页面", str(raised.exception))

    def test_channels_ai_annotation_is_selected_and_read_back_before_publish(self):
        page = MagicMock()
        publish_root = MagicMock()
        control = MagicMock()
        selected_control = MagicMock()
        option = MagicMock()
        control.inner_text.return_value = "选择视频标注"
        selected_control.inner_text.return_value = "含AI生成内容"
        with (
            patch.object(ChannelsService, "_find_video_annotation_control", side_effect=[(control, False), (selected_control, False)]),
            patch.object(ChannelsService, "_first_visible", return_value=option),
        ):
            result = ChannelsService._apply_video_annotation(page, publish_root, "ai_generated")
        self.assertEqual(result, "含AI生成内容")
        control.click.assert_called_once()
        option.click.assert_called_once()

    def test_channels_annotation_stops_publish_when_readback_does_not_match(self):
        page = MagicMock()
        publish_root = MagicMock()
        control = MagicMock()
        unchanged_control = MagicMock()
        option = MagicMock()
        control.inner_text.return_value = "选择视频标注"
        unchanged_control.inner_text.return_value = "无需标注"
        with (
            patch.object(ChannelsService, "_find_video_annotation_control", side_effect=[(control, False), (unchanged_control, False)]),
            patch.object(ChannelsService, "_first_visible", return_value=option),
            self.assertRaises(ChannelsError) as raised,
        ):
            ChannelsService._apply_video_annotation(page, publish_root, "ai_generated")
        self.assertEqual(raised.exception.stage, "set_video_annotation")
        self.assertIn("未能回读", str(raised.exception))

    def test_channels_publish_uses_codec_capable_chrome(self):
        class FakeChromium:
            def __init__(self):
                self.options = None

            def launch(self, **options):
                self.options = options
                return "browser"

        class FakePlaywright:
            chromium = FakeChromium()

        with patch.dict(os.environ, {"CHANNELS_BROWSER_CHANNEL": "chrome"}):
            result = ChannelsService._launch_publish_browser(FakePlaywright())
        self.assertEqual(result, "browser")
        self.assertEqual(FakePlaywright.chromium.options["channel"], "chrome")
        self.assertIn("--disable-dev-shm-usage", FakePlaywright.chromium.options["args"])

    def test_channels_navigation_uses_commit_and_accepts_slow_dom_after_redirect(self):
        page = MagicMock()
        page.url = "https://channels.weixin.qq.com/platform/post/list"
        page.goto.side_effect = RuntimeError("Page.goto: Timeout 60000ms exceeded")

        ChannelsService._goto_creator_page(page, page.url)

        page.goto.assert_called_once_with(page.url, wait_until="commit", timeout=60000)
        page.wait_for_load_state.assert_called_once_with("domcontentloaded", timeout=15000)
        page.wait_for_timeout.assert_called_once_with(900)

    def test_channels_persistent_profile_is_seeded_from_verified_auth_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            auth_file = Path(temp_dir) / "account.json"
            auth_file.write_text("""{
              "cookies": [{"name": "sessionid", "value": "verified", "domain": ".weixin.qq.com", "path": "/"}],
              "origins": [{"origin": "https://channels.weixin.qq.com", "localStorage": [{"name": "account", "value": "ready"}]}]
            }""", encoding="utf-8")
            context = MagicMock()

            ChannelsService._restore_context_state(context, auth_file)

            context.add_cookies.assert_called_once()
            self.assertIn("channels.weixin.qq.com", context.add_init_script.call_args.args[0])
            self.assertIn("localStorage.setItem", context.add_init_script.call_args.args[0])

    def test_channels_navigation_error_recognizes_context_replacement(self):
        self.assertTrue(ChannelsService._transient_navigation_error(
            RuntimeError("Locator.count: Execution context was destroyed, most likely because of a navigation")
        ))
        self.assertFalse(ChannelsService._transient_navigation_error(RuntimeError("permission denied")))

    def test_channels_publish_page_enters_from_creator_home_before_upload(self):
        service = ChannelsService()
        page = MagicMock()
        page.url = "https://channels.weixin.qq.com/platform"
        page.frames = []
        page.main_frame = MagicMock()
        page.get_by_text.return_value.first.count.return_value = 0
        publish_root = MagicMock()
        file_input = MagicMock()
        file_input.count.return_value = 1
        with patch.object(service, "_goto_creator_page") as navigate, patch.object(
            service, "_publish_root_and_input", return_value=(publish_root, file_input),
        ):
            result_root, result_input = service._open_publish_page(page)
        navigate.assert_called_once_with(page, "https://channels.weixin.qq.com/platform", timeout_ms=60000)
        self.assertIs(result_root, publish_root)
        self.assertIs(result_input, file_input)

    def test_channels_readback_only_marks_explicit_export_id_verified(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/export-proof.mp4", filename="export-proof.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="export-proof-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/export-proof.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="export-proof-task", batch_id="export-proof-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="加热标识核验", status="submitted",
            )
            db.add(task)
            db.flush()
            _apply_channels_readback(db, task, {
                "found": True,
                "published": True,
                "publication_state": "published",
                "platform_content_id": "generic-object-id",
                "platform_export_id": "verified-export-id",
                "platform_export_source": "creator_api",
                "message": "已确认公开",
            }, "2026-08-20")
            db.commit()
            self.assertEqual(task.platform_content_id, "generic-object-id")
            self.assertEqual(task.platform_export_id, "verified-export-id")
            self.assertEqual(task.platform_export_source, "creator_api")
            self.assertIsNotNone(task.platform_export_verified_at)

    def test_channels_readback_rejects_identity_already_bound_to_another_task(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/identity.mp4", filename="identity.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="identity-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/identity.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            owner = ChannelsDelivery(
                id="identity-owner", batch_id="identity-batch", asset_id=asset.id,
                created_by_number="FD-1", account_id=account.id, account_name=account.nickname,
                title="第一条短标题", status="success", platform_content_id="content-one",
                platform_export_id="export/one",
            )
            duplicate = ChannelsDelivery(
                id="identity-duplicate", batch_id="identity-batch", asset_id=asset.id,
                created_by_number="FD-1", account_id=account.id, account_name=account.nickname,
                title="第二条短标题", status="submitted",
            )
            db.add_all([owner, duplicate])
            db.flush()
            _apply_channels_readback(db, duplicate, {
                "found": True,
                "published": True,
                "publication_state": "published",
                "platform_content_id": "content-one",
                "platform_export_id": "export/one",
                "view_count": 999,
            }, "2026-08-21")
            self.assertEqual(duplicate.status, "submitted")
            self.assertEqual(duplicate.platform_content_id, "")
            self.assertIsNone(duplicate.view_count)
            self.assertIn("停止误匹配", duplicate.message)

    def test_channels_public_custom_cover_waits_until_list_cover_is_visible(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cover-check.mp4", filename="cover-check.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="cover-check-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/cover-check.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="cover-check-task", batch_id="cover-check-batch", asset_id=asset.id,
                created_by_number="FD-1", account_id=account.id, account_name=account.nickname,
                title="封面核验短标题", status="submitted", cover_object_key="yxb/cover.jpg",
            )
            db.add(task)
            db.flush()
            _apply_channels_readback(db, task, {
                "found": True,
                "published": True,
                "publication_state": "published",
                "platform_content_id": "cover-content",
                "cover_present": True,
            }, "2026-08-21")
            self.assertEqual(task.status, "success")
            self.assertEqual(task.failure_stage, "set_cover")
            self.assertIn("不会重复发布", task.message)
            self.assertIsNotNone(task.published_at)

            # An arbitrary visible <img> above must not pass custom-cover QA.
            self.assertEqual(task.cover_status, "unverified")
            _apply_channels_readback(db, task, {"found": True, "published": True,
                "platform_content_id": "cover-content", "cover_status": "mismatch",
                "cover_message": "平台封面不同"}, "2026-08-21")
            self.assertEqual(task.status, "success")
            self.assertEqual(task.cover_status, "mismatch")
            self.assertIn("未生效", task.message)
            _apply_channels_readback(db, task, {"found": True, "published": True,
                "platform_content_id": "cover-content", "cover_status": "verified",
                "cover_message": "图片一致"}, "2026-08-21")
            self.assertEqual(task.cover_status, "verified")
            self.assertEqual(task.failure_stage, "")
            self.assertIn("所选图片一致", task.message)

    def test_channels_root_publication_does_not_claim_custom_cover_is_visible(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/root-cover.mp4", filename="root-cover.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="root-cover-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/root-cover.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="root-cover-task", batch_id="root-cover-batch", asset_id=asset.id,
                created_by_number="FD-1", account_id=account.id, account_name=account.nickname,
                title="根数据封面核验", status="submitted", cover_object_key="yxb/root-cover.jpg",
            )
            db.add(task)
            db.flush()
            _apply_channels_readback(db, task, {
                "found": True,
                "published": True,
                "publication_state": "published",
                "platform_content_id": "root-cover-content",
                "source": "fandow_root_channels",
            }, "2026-08-21")
            self.assertEqual(task.status, "success")
            self.assertEqual(task.failure_stage, "set_cover")
            self.assertIn("继续核验自定义封面", task.message)
            self.assertIsNotNone(task.published_at)

    def test_channels_public_video_keeps_polling_until_custom_cover_is_verified(self):
        now = datetime.utcnow()
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/cover-poll.mp4", filename="cover-poll.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="cover-poll-account", owner_number="FD-1", owner_name="甲",
                nickname="甲视频号", auth_file="/data/channels-auth/cover-poll.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add(ChannelsDelivery(
                id="cover-poll-task", batch_id="cover-poll-batch", asset_id=asset.id,
                created_by_number="FD-1", account_id=account.id, account_name=account.nickname,
                title="已公开封面待核验", status="success", failure_stage="set_cover",
                published_at=now - timedelta(minutes=10), updated_at=now - timedelta(minutes=10),
            ))
            db.commit()
        with patch("app.main._readback_channels_tasks", return_value={"processed": 1, "errors": 0}) as readback:
            result = run_channels_confirmation_if_due(now=now)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(readback.call_args.args[0], ["cover-poll-task"])

    def test_channels_promotion_mappings_and_payload_are_exact(self):
        payload = ChannelsPromotionService.build_create_payload(
            "export-1", "net_deal_roi", 3000, 24, "测试加热计划",
        )
        self.assertEqual(PROMOTION_TARGETS, {
            "play": 1,
            "follow": 5,
            "like": 6,
            "product_click": 7,
            "click": 7,
            "product_pay": 8,
            "deal_roi": 11,
            "smart": 14,
            "net_deal_roi": 40,
            "net_product_pay": 42,
            "heart": 43,
        })
        self.assertEqual(PROMOTION_DURATIONS, {6: 21600, 8: 28800, 12: 43200, 24: 86400})
        self.assertEqual(payload["materialExportIds"], ["export-1"])
        self.assertEqual(payload["feedPromotionOrderInfo"]["promotionTarget"], 40)
        self.assertEqual(payload["feedPromotionOrderInfo"]["promotionType"], 1)
        self.assertEqual(payload["feedPromotionOrderInfo"]["estimatedWecoinAmount"], "3000")
        self.assertEqual(payload["feedPromotionOrderInfo"]["duration"], "86400")
        self.assertEqual(payload["orderName"], "测试加热计划")

    def test_channels_promotion_prepare_reads_tencent_enterprise_user_type(self):
        service = ChannelsPromotionService()
        with patch.object(service, "_transfer", return_value={
            "data": {
                "baseResp": {"errcode": 0},
                "personalUserInfo": {"uniqId": "enterprise-uniq", "nickname": "广州慕可生物科技有限公司"},
                "userWecoinInfo": {"userType": 3, "balance": "1200"},
            },
        }):
            prepared = service.get_user_prepare("secret-cookie")
        self.assertEqual(prepared["user_type"], 3)
        self.assertEqual(prepared["account_type"], "企业账户")
        self.assertTrue(prepared["is_enterprise"])
        self.assertEqual(prepared["balance"], 1200)

    def test_channels_promotion_accepts_independent_enterprise_account_and_rejects_personal(self):
        service = ChannelsPromotionService()
        service.require_enterprise_account({
            "nickname": "广州慕可生物科技有限公司",
            "user_type": 3,
        })
        with self.assertRaises(ChannelsPromotionError) as caught:
            service.require_enterprise_account({"nickname": "不不知归鹿", "user_type": 1})
        self.assertEqual(caught.exception.code, "enterprise_required")
        self.assertIn("Apple 个人账户", str(caught.exception))

        owner = self.user("FD-1", "甲")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/mismatch.mp4", filename="mismatch.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="mismatch-account", owner_number="FD-1", owner_name="甲",
                nickname="WIS品牌精选", auth_file="/data/channels-auth/mismatch.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            db.add_all([
                ChannelsDelivery(
                    id="mismatch-task", batch_id="mismatch-batch", asset_id=asset.id,
                    created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                    account_name=account.nickname, title="公开视频", status="success",
                    published_at=datetime.utcnow(), platform_export_id="export-mismatch",
                    platform_export_source="root", platform_export_verified_at=datetime.utcnow(),
                ),
                ChannelsPromotionAccount(
                    account_id=account.id, owner_number="FD-1", auth_file="/data/channels-promotion-auth/mismatch.json",
                    status="active", nickname="广州慕可生物科技有限公司", user_type=3, balance_wecoin=1000,
                    authorized_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(hours=1),
                ),
            ])
            db.commit()
            with patch.object(channels_promotion_service, "quote", return_value={
                "ok": True, "need_pay": 100, "balance": 1000, "quote_fallback": False,
                "nickname": "广州慕可生物科技有限公司", "user_type": 3, "account_type": "企业账户",
            }) as quote:
                result = channels_promotion_quote(
                    "mismatch-task",
                    ChannelsPromotionQuote(promotion_target="play", budget_wecoin=100, duration_hours=6),
                    db,
                    owner,
                )
            self.assertEqual(result["nickname"], "广州慕可生物科技有限公司")
            quote.assert_called_once()

            promotion = db.get(ChannelsPromotionAccount, account.id)
            promotion.user_type = 1
            promotion.nickname = "不不知归鹿"
            db.commit()
            with self.assertRaises(HTTPException) as blocked:
                channels_promotion_quote(
                    "mismatch-task",
                    ChannelsPromotionQuote(promotion_target="play", budget_wecoin=100, duration_hours=6),
                    db,
                    owner,
                )
            self.assertEqual(blocked.exception.status_code, 409)
            self.assertIn("企业账户", blocked.exception.detail)

    def test_channels_promotion_quote_checks_real_balance_and_never_exposes_cookie(self):
        service = ChannelsPromotionService()
        with tempfile.TemporaryDirectory() as directory:
            service.auth_root = Path(directory)
            auth_file = service._write_cookie_file("FD-1", "promotion-account", "secret-cookie")
            account = ChannelsPromotionAccount(
                account_id="promotion-account", owner_number="FD-1", auth_file=str(auth_file),
                status="active", nickname="企业加热账户", user_type=3,
                expires_at=datetime.utcnow() + timedelta(hours=1),
            )
            with patch.object(service, "get_user_prepare", return_value={
                "uniq_id": "uniq-1", "nickname": "企业加热账户", "balance": 80,
                "user_type": 3, "account_type": "企业账户", "is_enterprise": True,
            }), patch.object(service, "_transfer", return_value={
                "data": {"baseResp": {"errcode": 0}, "payDetail": {"needPayAmountInCents": 1000}},
            }):
                with self.assertRaises(ChannelsPromotionError) as caught:
                    service.quote(account, "export-1", "play", 100, 24)
            self.assertEqual(caught.exception.code, "insufficient_balance")
            self.assertNotIn("secret-cookie", str(caught.exception))

    def test_channels_promotion_quote_converts_tencent_tenths_of_wecoin(self):
        service = ChannelsPromotionService()
        account = ChannelsPromotionAccount(
            account_id="promotion-account", owner_number="FD-1", auth_file="unused",
            status="active", nickname="企业加热账户", user_type=3,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        with patch.object(service, "prepare_account", return_value={
            "cookie": "secret", "balance": 1000, "nickname": "企业加热账户",
            "user_type": 3, "account_type": "企业账户",
        }), patch.object(service, "_transfer", return_value={
            "data": {"baseResp": {"errcode": 0}, "payDetail": {"needPayAmountInCents": 3291}},
        }):
            result = service.quote(account, "export-1", "play", 330, 24)
        self.assertEqual(result["need_pay"], 330)

    def test_channels_promotion_uses_another_active_enterprise_account_for_historical_video(self):
        owner = self.user("FD-1", "甲")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/historical.mp4", filename="historical.mp4", media_type="video", size=1)
            publishing_account = ChannelsAccount(
                id="historical-publishing-account", owner_number="FD-1", owner_name="甲",
                nickname="历史发布视频号", auth_file="/data/channels-auth/historical.json", status="expired",
            )
            heating_host = ChannelsAccount(
                id="enterprise-heating-host", owner_number="FD-1", owner_name="甲",
                nickname="当前登录视频号", auth_file="/data/channels-auth/current.json", status="active",
            )
            db.add_all([asset, publishing_account, heating_host])
            db.flush()
            db.add_all([
                ChannelsDelivery(
                    id="historical-promote-task", batch_id="historical-promote-batch", asset_id=asset.id,
                    created_by_number="FD-1", created_by_name="甲", account_id=publishing_account.id,
                    account_name=publishing_account.nickname, title="历史公开视频", status="success",
                    published_at=datetime.utcnow(), platform_export_id="export-historical",
                    platform_export_source="root", platform_export_verified_at=datetime.utcnow(),
                ),
                ChannelsPromotionAccount(
                    account_id=heating_host.id, owner_number="FD-1",
                    auth_file="/data/channels-promotion-auth/enterprise.json", status="active",
                    nickname="企业加热账户", user_type=3, balance_wecoin=1000,
                    authorized_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(hours=1),
                ),
            ])
            db.commit()

            quote_payload = ChannelsPromotionQuote(
                promotion_target="play", budget_wecoin=100, duration_hours=6,
            )
            with patch.object(channels_promotion_service, "quote", return_value={
                "ok": True, "need_pay": 100, "balance": 1000, "quote_fallback": False,
                "nickname": "企业加热账户", "user_type": 3, "account_type": "企业账户",
            }) as quote:
                result = channels_promotion_quote(
                    "historical-promote-task", quote_payload, db, owner,
                )
            self.assertEqual(result["need_pay"], 100)
            self.assertEqual(quote.call_args.args[0].account_id, heating_host.id)

            create_payload = ChannelsPromotionOrderCreate(
                promotion_target="play", budget_wecoin=100, duration_hours=6,
                order_name="跨账户测试计划", confirmed=True, idempotency_key="cross-account-idem",
            )
            with patch.object(channels_promotion_service, "quote", return_value={
                "ok": True, "need_pay": 100, "balance": 1000, "quote_fallback": False,
                "nickname": "企业加热账户", "user_type": 3, "account_type": "企业账户",
            }), patch.object(channels_promotion_service, "create_promotion", return_value={
                "promotion_id": "cross-account-promotion",
                "response_summary": "{}",
            }):
                created = channels_promotion_order_create(
                    "historical-promote-task", create_payload, db, owner,
                )
            self.assertEqual(created["status"], "pending_payment")
            self.assertNotIn("pc_sdk_info", created)
            saved = db.scalar(select(ChannelsPromotionOrder).where(
                ChannelsPromotionOrder.delivery_id == "historical-promote-task",
            ))
            self.assertEqual(saved.account_id, heating_host.id)

    def test_channels_promotion_create_returns_payment_session_not_false_paid_success(self):
        service = ChannelsPromotionService()
        account = ChannelsPromotionAccount(
            account_id="promotion-account", owner_number="FD-1", auth_file="unused",
            status="active", nickname="企业加热账户", user_type=3,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        with patch.object(service, "prepare_account", return_value={"cookie": "secret", "balance": 1000}), patch.object(
            service,
            "_transfer",
            side_effect=[
                {"data": {"baseResp": {"errcode": 0}, "promotionId": "promotion-100"}},
                {"data": {"baseResp": {"errcode": 0}, "pcSdkInfo": "sdk=session-one"}},
            ],
        ) as transfer:
            result = service.create_and_prepare_payment(account, "export-1", "play", 100, 6, "测试")
        self.assertEqual(result["promotion_id"], "promotion-100")
        self.assertEqual(result["pc_sdk_info"], "sdk=session-one")
        self.assertNotIn("cost_wecoin", result)
        self.assertEqual(transfer.call_count, 2)

        with patch.object(service, "prepare_account", return_value={"cookie": "secret", "balance": 1000}), patch.object(
            service,
            "_transfer",
            side_effect=ChannelsPromotionError("创建失败", "create_failed"),
        ) as transfer:
            with self.assertRaises(ChannelsPromotionError):
                service.create_and_prepare_payment(account, "export-1", "play", 100, 6, "测试")
        self.assertEqual(transfer.call_count, 1)

    def test_channels_promotion_detail_keeps_unpaid_callback_order_pending(self):
        service = ChannelsPromotionService()
        account = ChannelsPromotionAccount(
            account_id="promotion-account", owner_number="FD-1", auth_file="unused",
            status="active", nickname="企业加热账户", user_type=3,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        with patch.object(service, "prepare_account", return_value={"cookie": "secret", "balance": 10114548}), patch.object(
            service,
            "_transfer",
            return_value={"data": {"baseResp": {"errcode": 0}, "order": {"orderInfo": {
                "status": 1,
                "billingMethod": 0,
                "paymentInfo": {
                    "paidWecoinAmount": 3000,
                    "wecoinInfo": {"payAmount": "3000", "payStatus": 2},
                },
            }}}},
        ):
            result = service.get_order_detail(account, "promotion-unpaid")
        self.assertEqual(result["status"], "pending_payment")
        self.assertIsNone(result["cost_wecoin"])
        self.assertEqual(result["wecoin_pay_status"], 2)

    def test_channels_promotion_detail_only_marks_paid_after_platform_confirmation(self):
        service = ChannelsPromotionService()
        account = ChannelsPromotionAccount(
            account_id="promotion-account", owner_number="FD-1", auth_file="unused",
            status="active", nickname="企业加热账户", user_type=3,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        with patch.object(service, "prepare_account", return_value={"cookie": "secret", "balance": 10111548}), patch.object(
            service,
            "_transfer",
            return_value={"data": {"baseResp": {"errcode": 0}, "order": {"orderInfo": {
                "status": 5,
                "billingMethod": 0,
                "paymentInfo": {"wecoinInfo": {"payAmount": "3000", "payStatus": 1}},
            }}}},
        ):
            result = service.get_order_detail(account, "promotion-paid")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["cost_wecoin"], 3000)

    def test_channels_promotion_api_is_owner_scoped_confirmed_and_idempotent(self):
        owner = self.user("FD-1", "甲")
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/promote.mp4", filename="promote.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="promote-account", owner_number="FD-1", owner_name="甲", nickname="甲视频号",
                auth_file="/data/channels-auth/promote.json", status="active",
            )
            db.add_all([asset, account])
            db.flush()
            task = ChannelsDelivery(
                id="promote-task", batch_id="promote-batch", asset_id=asset.id,
                created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                account_name=account.nickname, title="可加热视频", status="success",
                published_at=datetime.utcnow(), platform_export_id="export-verified",
                platform_export_source="creator_api", platform_export_verified_at=datetime.utcnow(),
            )
            promotion_account = ChannelsPromotionAccount(
                account_id=account.id, owner_number="FD-1", auth_file="/data/channels-promotion-auth/promote.json",
                status="active", nickname="独立企业加热账户", user_type=3, balance_wecoin=1000,
                authorized_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(hours=1),
            )
            db.add_all([task, promotion_account])
            db.commit()

            quote_payload = ChannelsPromotionQuote(promotion_target="play", budget_wecoin=100, duration_hours=6)
            with patch.object(channels_promotion_service, "quote", return_value={
                "ok": True, "need_pay": 100, "balance": 1000, "quote_fallback": False, "nickname": "甲视频号",
            }):
                quote_result = channels_promotion_quote("promote-task", quote_payload, db, owner)
            self.assertEqual(quote_result["need_pay"], 100)

            with self.assertRaises(HTTPException) as unconfirmed:
                channels_promotion_order_create(
                    "promote-task",
                    ChannelsPromotionOrderCreate(
                        promotion_target="play", budget_wecoin=100, duration_hours=6,
                        order_name="测试计划", confirmed=False, idempotency_key="idem-123456",
                    ),
                    db,
                    owner,
                )
            self.assertEqual(unconfirmed.exception.status_code, 409)

            create_payload = ChannelsPromotionOrderCreate(
                promotion_target="play", budget_wecoin=100, duration_hours=6,
                order_name="测试计划", confirmed=True, idempotency_key="idem-123456",
            )
            with patch.object(channels_promotion_service, "quote", return_value={
                "ok": True, "need_pay": 100, "balance": 1000, "quote_fallback": False, "nickname": "甲视频号",
            }), patch.object(channels_promotion_service, "create_promotion", return_value={
                "promotion_id": "promotion-1", "response_summary": "{}",
            }) as create:
                first = channels_promotion_order_create("promote-task", create_payload, db, owner)
                duplicate = channels_promotion_order_create("promote-task", create_payload, db, owner)
            self.assertEqual(first["status"], "pending_payment")
            self.assertTrue(duplicate["duplicate_request"])
            self.assertEqual(create.call_count, 1)
            self.assertEqual(db.scalar(select(ChannelsPromotionOrder).where(
                ChannelsPromotionOrder.delivery_id == "promote-task",
            )).promotion_id, "promotion-1")

            with self.assertRaises(HTTPException) as forbidden:
                channels_promotion_orders("promote-task", db, self.user("FD-2", "乙"))
            self.assertEqual(forbidden.exception.status_code, 404)

    def test_channels_payment_session_is_idempotent_encrypted_and_remote_confirmed(self):
        owner = self.user("FD-1", "甲")
        with tempfile.TemporaryDirectory() as directory, SessionLocal() as db, patch.object(
            channels_promotion_service, "auth_root", Path(directory),
        ):
            asset = Asset(object_key="yxb/payment.mp4", filename="payment.mp4", media_type="video", size=1)
            account = ChannelsAccount(
                id="payment-account", owner_number="FD-1", owner_name="甲", nickname="发布视频号",
                auth_file="/data/channels-auth/payment.json", status="active",
            )
            db.add_all([asset, account]); db.flush()
            db.add_all([
                ChannelsDelivery(
                    id="payment-task", batch_id="payment-batch", asset_id=asset.id,
                    created_by_number="FD-1", created_by_name="甲", account_id=account.id,
                    account_name=account.nickname, title="已公开视频", status="success",
                    published_at=datetime.utcnow(), platform_export_id="export-payment",
                    platform_export_source="creator_api", platform_export_verified_at=datetime.utcnow(),
                ),
                ChannelsPromotionAccount(
                    account_id=account.id, owner_number="FD-1", auth_file="unused",
                    status="active", nickname="企业加热账户", user_type=3, balance_wecoin=1000,
                    authorized_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(hours=1),
                ),
            ])
            db.flush()
            order = ChannelsPromotionOrder(
                id="payment-order", delivery_id="payment-task", account_id=account.id,
                created_by_number="FD-1", created_by_name="甲", platform_export_id="export-payment",
                promotion_target="play", budget_wecoin=330, quoted_wecoin=330, duration_hours=24,
                order_name="支付安全测试", idempotency_key="order-idempotency", status="pending_payment",
                promotion_id="promotion-payment", request_snapshot={"input": {
                    "promotion_target": "play", "budget_wecoin": 330, "duration_hours": 24,
                    "funding_type": "wecoin", "bid_mode": "volume", "start_mode": "immediate",
                    "billing_method": "prepaid", "promotion_mode": "smart",
                    "portrait_mode": "none", "voucher_mode": "none",
                }}, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
            )
            db.add(order); db.commit()

            pending = {
                "status": "pending_payment", "cost_wecoin": None, "balance": 1000,
                "message": "待支付", "response_summary": "{}",
            }
            with patch.object(channels_promotion_service, "get_order_detail", return_value=pending) as detail, patch.object(
                channels_promotion_service, "quote", return_value={
                    "ok": True, "need_pay": 330, "balance": 1000, "quote_fallback": False,
                },
            ), patch.object(channels_promotion_service, "payment_session", return_value={
                "promotion_id": "promotion-payment", "pc_sdk_info": "secret-sdk-token",
                "response_summary": "{}",
            }) as prepare:
                response = channels_promotion_payment_session_create(
                    "payment-task", "payment-order",
                    ChannelsPromotionPaymentSessionCreate(request_token="payment-token-1"), db, owner,
                )
                duplicate = channels_promotion_payment_session_create(
                    "payment-task", "payment-order",
                    ChannelsPromotionPaymentSessionCreate(request_token="payment-token-1"), db, owner,
                )
            payload = json.loads(response.body)
            duplicate_payload = json.loads(duplicate.body)
            self.assertEqual(payload["id"], duplicate_payload["id"])
            self.assertIn("webeans.my-qcloud.com", payload["launch_url"])
            self.assertNotIn("pc_sdk_info", payload)
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(detail.call_count, 1)
            session = db.get(ChannelsPromotionPaymentSession, payload["id"])
            self.assertNotIn("secret-sdk-token", session.pc_sdk_info_ciphertext)

            advisory = channels_promotion_payment_session_event(
                "payment-task", "payment-order", session.id,
                ChannelsPromotionPaymentEvent(action="consumeSuccess"), db, owner,
            )
            self.assertEqual(json.loads(advisory.body)["status"], "confirming")
            self.assertNotEqual(db.get(ChannelsPromotionOrder, order.id).status, "success")

            paid = {
                "status": "success", "cost_wecoin": 330, "balance": 670,
                "message": "已支付", "response_summary": "{}",
            }
            with patch.object(channels_promotion_service, "get_order_detail", return_value=paid):
                verified = channels_promotion_payment_session_get(
                    "payment-task", "payment-order", session.id, db, owner,
                )
            self.assertEqual(json.loads(verified.body)["status"], "succeeded")
            self.assertEqual(db.get(ChannelsPromotionOrder, order.id).status, "success")
            self.assertEqual(db.get(ChannelsPromotionOrder, order.id).cost_wecoin, 330)


if __name__ == "__main__":
    unittest.main()
