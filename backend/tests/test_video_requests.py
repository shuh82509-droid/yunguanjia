import os
import unittest
from unittest.mock import Mock, patch

from fastapi import BackgroundTasks, HTTPException

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.database import Base, SessionLocal, engine
from app.main import (
    _reference_preview_active,
    _reference_preview_lock,
    VideoRequestAssign,
    VideoRequestCreate,
    VideoRequestDeliver,
    VideoRequestFeedback,
    VideoRequestReturn,
    ensure_asset_schema,
    notifications_list,
    run_feishu_notification_batch,
    user_permissions,
    is_video_request_assigner,
    video_request_assignees,
    video_request_accept,
    video_request_assign,
    video_request_create,
    video_request_deliver,
    video_request_list,
    video_request_reference_previews,
    video_request_revision,
    video_request_return,
    video_request_start,
)
from app.feishu_notification_service import FeishuNotificationService, FeishuSendResult
from app.models import Asset, OaAccessGrant, UserNotification, VideoRequest, VideoRequestDelivery, VideoRequestEvent


class VideoRequestWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        ensure_asset_schema()

    def setUp(self):
        # Unit tests must not depend on workstation OSS credentials.
        url_patch = patch("app.main.oss_service.url_for", side_effect=lambda key, **kwargs: f"https://oss.example/{key}" if key else "")
        url_patch.start()
        self.addCleanup(url_patch.stop)
        with SessionLocal() as db:
            db.query(UserNotification).delete()
            db.query(VideoRequestEvent).delete()
            db.query(VideoRequestDelivery).delete()
            db.query(VideoRequest).delete()
            db.query(Asset).delete()
            db.query(OaAccessGrant).delete()
            for number, name in [
                ("FD-WORKER", "制作同事"), ("FD-BATCH-WORKER", "批量制作"),
                ("FD-REVIEW-WORKER", "制作同事"), ("FD-TWO", "乙同事"),
            ]:
                db.add(OaAccessGrant(identifier=number, identifier_type="number", user_number=number,
                                     real_name=name, department="品牌营销部", center="视频中心", active=True))
            db.commit()

    @staticmethod
    def user(number: str, name: str) -> dict:
        return {"number": number, "realName": name, "groupName": "品牌营销部"}

    def test_create_retry_reuses_receipt_without_duplicate_work(self):
        requester = self.user("FD-RETRY", "提需同事")
        payload = VideoRequestCreate(
            client_request_id="browser-retry-1",
            product="通用",
            description="制作一条产品展示视频",
        )
        with SessionLocal() as db:
            first = video_request_create(payload, db=db, user=requester)
            second = video_request_create(payload, db=db, user=requester)
            self.assertEqual(second["id"], first["id"])
            self.assertEqual(db.query(VideoRequest).count(), 1)
            self.assertEqual(db.query(VideoRequestEvent).count(), 1)
            self.assertEqual(db.query(UserNotification).count(), 1)
            with self.assertRaises(HTTPException) as changed:
                video_request_create(
                    VideoRequestCreate(
                        client_request_id="browser-retry-1",
                        product="通用",
                        description="不同的需求内容",
                    ),
                    db=db,
                    user=requester,
                )
            self.assertEqual(changed.exception.status_code, 409)
            self.assertEqual(db.query(VideoRequest).count(), 1)

    def test_assigner_full_revision_flow_notifies_people(self):
        requester = self.user("FD-REQUESTER", "提需同事")
        supervisor = self.user("FD-SUPERVISOR", "何雨庭")
        worker = self.user("FD-WORKER", "制作同事")
        with patch.dict(os.environ, {"VIDEO_REQUEST_ASSIGNER_NAMES": "何雨庭"}, clear=False), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(product="水润面膜", description="制作一条 15 秒卖点视频"),
                db=db,
                user=requester,
            )
            request_id = created["id"]
            self.assertEqual(created["status"], "submitted")
            self.assertEqual(notifications_list(limit=20, db=db, user=supervisor)["unread"], 1)
            with patch("app.main.feishu_notification_service.send", return_value=FeishuSendResult("sent")):
                self.assertEqual(run_feishu_notification_batch(), 1)
            external = db.query(UserNotification).filter_by(recipient_name="何雨庭").one()
            db.refresh(external)
            self.assertEqual(external.external_status, "sent")
            self.assertIsNotNone(external.external_sent_at)

            with self.assertRaises(HTTPException) as denied:
                video_request_assign(
                    request_id,
                    VideoRequestAssign(assignee_number="FD-WORKER", assignee_name="制作同事"),
                    db=db,
                    user=requester,
                )
            self.assertEqual(denied.exception.status_code, 403)

            assigned = video_request_assign(
                request_id,
                VideoRequestAssign(assignee_number="FD-WORKER", assignee_name="制作同事"),
                db=db,
                user=supervisor,
            )
            self.assertEqual(assigned["status"], "assigned")
            self.assertEqual(notifications_list(limit=20, db=db, user=worker)["unread"], 1)
            assignment_notice = db.query(UserNotification).filter_by(
                recipient_number="FD-WORKER", title="视频制作任务 · 水润面膜"
            ).one()
            self.assertIn("提需人：提需同事", assignment_notice.message)
            self.assertIn("制作一条 15 秒卖点视频", assignment_notice.message)
            self.assertEqual(video_request_start(request_id, db=db, user=worker)["status"], "in_production")

            asset = Asset(
                object_key="yxb/uploads/worker/final-v1.mp4",
                filename="final-v1.mp4",
                media_type="video",
                size=1024,
                asset_scope="marketing_video",
                uploaded_by_number="FD-WORKER",
                uploaded_by_name="制作同事",
                category="水润面膜",
                content_type="产品展示",
                library_type="source",
                asset_subtype="AI原创素材",
            )
            db.add(asset)
            db.commit()
            delivered = video_request_deliver(
                request_id,
                VideoRequestDeliver(asset_id=asset.id, note="第一版"),
                db=db,
                user=worker,
            )
            self.assertEqual(delivered["status"], "delivered")
            self.assertEqual(delivered["delivery_version"], 1)
            db.refresh(asset)
            self.assertEqual(asset.asset_scope, "marketing_video")
            self.assertEqual(asset.library_type, "source")
            self.assertEqual(asset.asset_subtype, "AI原创素材")
            self.assertEqual(asset.category, "水润面膜")
            self.assertEqual(notifications_list(limit=20, db=db, user=requester)["unread"], 1)
            delivery_notice = db.query(UserNotification).filter_by(
                recipient_number="FD-REQUESTER", title="成片待验收 · 水润面膜"
            ).one()
            self.assertIn("制作人：制作同事", delivery_notice.message)
            self.assertIn("本批成片：1 条", delivery_notice.message)

            revised = video_request_revision(
                request_id,
                VideoRequestFeedback(feedback="前三秒改快，产品露出提前"),
                db=db,
                user=requester,
            )
            self.assertEqual(revised["status"], "revision_requested")
            self.assertEqual(revised["latest_feedback"], "前三秒改快，产品露出提前")
            self.assertGreaterEqual(notifications_list(limit=20, db=db, user=worker)["unread"], 2)

    def test_supervisor_can_return_with_reason_and_requester_is_notified(self):
        requester = self.user("FD-RETURN", "提需同事")
        supervisor = self.user("FD-SUPERVISOR", "何雨庭")
        with patch.dict(os.environ, {"VIDEO_REQUEST_ASSIGNER_NAMES": "何雨庭"}, clear=False), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(product="通用", description="与现有需求重复"), db=db, user=requester
            )
            returned = video_request_return(
                created["id"], VideoRequestReturn(reason="重复需求，无需再次制作"), db=db, user=supervisor
            )
            self.assertEqual(returned["status"], "returned")
            self.assertEqual(returned["return_reason"], "重复需求，无需再次制作")
            self.assertEqual(returned["progress_percent"], 100)
            self.assertEqual(notifications_list(limit=20, db=db, user=requester)["unread"], 1)

    def test_batch_delivery_preserves_per_asset_source_classification(self):
        requester = self.user("FD-BATCH-OWNER", "批量提需")
        supervisor = self.user("FD-SUPERVISOR", "何雨庭")
        worker = self.user("FD-BATCH-WORKER", "批量制作")
        with patch.dict(os.environ, {"VIDEO_REQUEST_ASSIGNER_NAMES": "何雨庭"}, clear=False), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(product="通用", description="一条需求裂变多个品类"), db=db, user=requester
            )
            video_request_assign(
                created["id"],
                VideoRequestAssign(assignee_number="FD-BATCH-WORKER", assignee_name="批量制作"),
                db=db,
                user=supervisor,
            )
            assets = [
                Asset(
                    object_key="yxb/uploads/batch/eye.mp4", filename="eye.mp4", media_type="video", size=10,
                    uploaded_by_number="FD-BATCH-WORKER", uploaded_by_name="批量制作",
                    category="晶润眼膜", content_type="产品展示", library_type="source", asset_subtype="产品镜",
                ),
                Asset(
                    object_key="yxb/uploads/batch/mask.mp4", filename="mask.mp4", media_type="video", size=11,
                    uploaded_by_number="FD-BATCH-WORKER", uploaded_by_name="批量制作",
                    category="黑晶面膜", content_type="口播", library_type="source", asset_subtype="达人/KOC原片",
                ),
            ]
            db.add_all(assets)
            db.commit()
            delivered = video_request_deliver(
                created["id"],
                VideoRequestDeliver(asset_ids=[asset.id for asset in assets], note="多品类第一批"),
                db=db,
                user=worker,
            )
            self.assertEqual(delivered["status"], "delivered")
            self.assertEqual(len(delivered["latest_assets"]), 2)
            self.assertEqual([row["category"] for row in delivered["latest_assets"]], ["晶润眼膜", "黑晶面膜"])
            self.assertEqual(len({row["submission_id"] for row in delivered["deliveries"]}), 1)

    def test_delivered_request_exposes_review_only_to_requester_and_accepts_to_100_percent(self):
        requester = self.user("FD-REVIEW-OWNER", "验收同事")
        supervisor = self.user("FD-REVIEW-SUPERVISOR", "何雨庭")
        worker = self.user("FD-REVIEW-WORKER", "制作同事")
        with patch.dict(os.environ, {"VIDEO_REQUEST_ASSIGNER_NAMES": "何雨庭"}, clear=False), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(product="燕窝面膜", description="验收入口回归"), db=db, user=requester
            )
            video_request_assign(
                created["id"],
                VideoRequestAssign(assignee_number=worker["number"], assignee_name=worker["realName"]),
                db=db,
                user=supervisor,
            )
            asset = Asset(
                object_key="yxb/uploads/review/final.mp4", filename="final.mp4", media_type="video", size=10,
                uploaded_by_number=worker["number"], uploaded_by_name=worker["realName"],
                category="燕窝面膜", content_type="产品展示", library_type="source", asset_subtype="AI原创素材",
            )
            db.add(asset)
            db.commit()
            delivered = video_request_deliver(
                created["id"], VideoRequestDeliver(asset_id=asset.id, note="待验收"), db=db, user=worker
            )
            self.assertEqual(delivered["progress_percent"], 85)
            requester_page = video_request_list(
                scope="mine", status="all", q="", page=1, page_size=10, db=db, user=requester
            )
            self.assertTrue(requester_page["items"][0]["permissions"]["can_review"])
            self.assertEqual(len(requester_page["items"][0]["latest_assets"]), 1)
            supervisor_page = video_request_list(
                scope="all", status="all", q="", page=1, page_size=10, db=db, user=supervisor
            )
            self.assertFalse(supervisor_page["items"][0]["permissions"]["can_review"])
            accepted = video_request_accept(created["id"], db=db, user=requester)
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["progress_percent"], 100)

    def test_records_are_isolated_but_assignee_can_view_assignment(self):
        owner = self.user("FD-ONE", "甲同事")
        stranger = self.user("FD-TWO", "乙同事")
        supervisor = self.user("FD-SUPERVISOR", "何雨庭")
        with patch.dict(os.environ, {"VIDEO_REQUEST_ASSIGNER_NAMES": "何雨庭"}, clear=False), SessionLocal() as db:
            created = video_request_create(VideoRequestCreate(product="通用", description="测试需求"), db=db, user=owner)
            own_page = video_request_list(scope="mine", status="all", q="", page=1, page_size=10, db=db, user=owner)
            other_page = video_request_list(scope="mine", status="all", q="", page=1, page_size=10, db=db, user=stranger)
            self.assertEqual(own_page["total"], 1)
            self.assertEqual(other_page["total"], 0)
            video_request_assign(
                created["id"],
                VideoRequestAssign(assignee_number="FD-TWO", assignee_name="乙同事"),
                db=db,
                user=supervisor,
            )
            assigned_page = video_request_list(scope="assigned", status="all", q="", page=1, page_size=10, db=db, user=stranger)
            self.assertEqual(assigned_page["total"], 1)
            self.assertEqual(assigned_page["items"][0]["requester_name"], "甲同事")

    def test_list_supports_selectable_page_size(self):
        owner = self.user("FD-PAGE", "分页同事")
        with SessionLocal() as db:
            for index in range(7):
                video_request_create(
                    VideoRequestCreate(product="通用", description=f"需求 {index}"), db=db, user=owner
                )
            page = video_request_list(
                scope="mine", status="all", q="", page=1, page_size=5, db=db, user=owner
            )
            self.assertEqual(page["total"], 7)
            self.assertEqual(page["page_size"], 5)
            self.assertEqual(page["total_pages"], 2)
            self.assertEqual(len(page["items"]), 5)

    def test_list_groups_production_pending_and_completed_progress(self):
        owner = self.user("FD-PROGRESS", "进度同事")
        with SessionLocal() as db:
            waiting = video_request_create(
                VideoRequestCreate(product="通用", description="还没开始制作"), db=db, user=owner
            )
            completed = video_request_create(
                VideoRequestCreate(product="通用", description="已经完成制作"), db=db, user=owner
            )
            db.get(VideoRequest, completed["id"]).status = "accepted"
            db.commit()

            waiting_page = video_request_list(
                scope="mine", status="production_pending", q="", page=1, page_size=10, db=db, user=owner
            )
            completed_page = video_request_list(
                scope="mine", status="completed", q="", page=1, page_size=10, db=db, user=owner
            )

            self.assertEqual(waiting_page["total"], 1)
            self.assertEqual(waiting_page["items"][0]["id"], waiting["id"])
            self.assertEqual(completed_page["total"], 1)
            self.assertEqual(completed_page["items"][0]["id"], completed["id"])

    def test_share_text_is_accepted_and_task_progress_is_returned(self):
        requester = self.user("FD-SHARE", "分享同事")
        share_text = "3.21 复制打开抖音，查看参考视频 https://v.douyin.com/example/ 这条节奏很好"
        with SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(product="通用", description="", reference_url=share_text),
                db=db,
                user=requester,
            )
            self.assertEqual(created["reference_url"], share_text)
            self.assertEqual(created["progress_percent"], 10)
            self.assertIn("等待主管分配", created["progress_label"])

    def test_reference_images_are_validated_and_returned(self):
        requester = self.user("FD-IMAGE", "参考图同事")
        with patch("app.main.oss_service.head_asset", return_value={"media_type": "image", "size": 4096}), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(
                    product="通用",
                    description="按参考图片制作",
                    reference_images=[{
                        "object_key": "yxb/references/参考图同事/look-1.png",
                        "filename": "look-1.png",
                    }],
                ),
                db=db,
                user=requester,
            )
            self.assertEqual(len(created["reference_images"]), 1)
            self.assertEqual(created["reference_images"][0]["filename"], "look-1.png")

    def test_multiple_reference_videos_are_validated_and_returned(self):
        requester = self.user("FD-VIDEO", "参考视频同事")
        references = [
            {"object_key": "yxb/references/参考视频同事/look-1.mp4", "filename": "look-1.mp4"},
            {"object_key": "yxb/references/参考视频同事/look-2.mp4", "filename": "look-2.mp4"},
        ]
        with patch("app.main.oss_service.head_asset", return_value={"media_type": "video", "size": 12 * 1024 ** 2}), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(product="通用", description="按两条参考视频制作", reference_videos=references),
                db=db,
                user=requester,
            )
            self.assertEqual([row["filename"] for row in created["reference_videos"]], ["look-1.mp4", "look-2.mp4"])
            self.assertEqual(created["reference_video_key"], references[0]["object_key"])
            self.assertTrue(all(row["url"] for row in created["reference_videos"]))
            self.assertTrue(all(row["original_url"] for row in created["reference_videos"]))
            self.assertTrue(all(row["preview_status"] == "pending" for row in created["reference_videos"]))

    def test_ready_reference_preview_is_used_for_browser_playback(self):
        requester = self.user("FD-PREVIEW", "预览同事")
        original_key = "yxb/references/预览同事/hevc.mp4"
        preview_key = "yxb/reference-previews/preview.mp4"
        with patch("app.main.oss_service.head_asset", return_value={"media_type": "video", "size": 4096}), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(
                    product="通用",
                    description="浏览器兼容预览",
                    reference_videos=[{"object_key": original_key, "filename": "hevc.mp4"}],
                ),
                db=db,
                user=requester,
            )
            saved = db.get(VideoRequest, created["id"])
            saved.reference_videos = [{
                "object_key": original_key,
                "filename": "hevc.mp4",
                "preview_object_key": preview_key,
                "preview_status": "ready",
            }]
            db.commit()
            detail = video_request_list(
                scope="mine", status="all", q="", page=1, page_size=10, db=db, user=requester
            )["items"][0]
            video = detail["reference_videos"][0]
            self.assertIn("reference-previews", video["preview_url"])
            self.assertEqual(video["url"], video["preview_url"])
            self.assertIn("references", video["original_url"])
            self.assertEqual(video["preview_status"], "ready")

    def test_reference_preview_endpoint_schedules_one_visible_background_job(self):
        requester = self.user("FD-PREVIEW-JOB", "预览任务同事")
        with patch("app.main.oss_service.head_asset", return_value={"media_type": "video", "size": 4096}), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(
                    product="通用",
                    reference_videos=[{
                        "object_key": "yxb/references/预览任务同事/hevc.mp4",
                        "filename": "hevc.mp4",
                    }],
                ),
                db=db,
                user=requester,
            )
            background = BackgroundTasks()
            try:
                result = video_request_reference_previews(
                    created["id"], background_tasks=background, db=db, user=requester
                )
                self.assertEqual(result["status"], "queued")
                self.assertEqual(len(background.tasks), 1)
                second = video_request_reference_previews(
                    created["id"], background_tasks=BackgroundTasks(), db=db, user=requester
                )
                self.assertEqual(second["status"], "processing")
            finally:
                with _reference_preview_lock:
                    _reference_preview_active.discard(created["id"])

    def test_reference_video_rejects_files_larger_than_500mb(self):
        requester = self.user("FD-LARGE", "大文件同事")
        with patch("app.main.oss_service.head_asset", return_value={"media_type": "video", "size": 501 * 1024 ** 2}), SessionLocal() as db:
            with self.assertRaises(HTTPException) as denied:
                video_request_create(
                    VideoRequestCreate(
                        product="通用",
                        reference_videos=[{"object_key": "yxb/references/大文件同事/large.mp4", "filename": "large.mp4"}],
                    ),
                    db=db,
                    user=requester,
                )
            self.assertEqual(denied.exception.status_code, 400)
            self.assertIn("500MB", str(denied.exception.detail))

    def test_historical_single_reference_video_is_exposed_in_new_list(self):
        requester = self.user("FD-LEGACY", "历史同事")
        with patch("app.main.oss_service.head_asset", return_value={"media_type": "video", "size": 4096}), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(
                    product="通用",
                    reference_video_key="yxb/references/历史同事/legacy.mp4",
                    reference_video_name="legacy.mp4",
                ),
                db=db,
                user=requester,
            )
            saved = db.get(VideoRequest, created["id"])
            saved.reference_videos = []
            db.commit()
            page = video_request_list(scope="mine", status="all", q="", page=1, page_size=10, db=db, user=requester)
            self.assertEqual(page["items"][0]["reference_videos"][0]["filename"], "legacy.mp4")

    def test_super_admin_has_read_only_supervisor_task_view(self):
        requester = self.user("FD-OWNER", "提需同事")
        viewer = self.user("FD-OTHER-ADMIN", "其他管理员")
        with patch.dict(
            os.environ,
            {
                "SUPER_ADMIN_NUMBERS": "FD-OTHER-ADMIN",
                "VIDEO_REQUEST_ASSIGNER_NAMES": "何雨庭",
            },
            clear=False,
        ), SessionLocal() as db:
            video_request_create(
                VideoRequestCreate(product="通用", description="主管视图测试"),
                db=db,
                user=requester,
            )
            page = video_request_list(
                scope="all", status="all", q="", page=1, page_size=10, db=db, user=viewer
            )
            self.assertEqual(page["total"], 1)
            self.assertTrue(user_permissions(viewer)["video_request_supervisor_viewer"])
            self.assertTrue(page["items"][0]["permissions"]["can_view_supervisor"])
            self.assertFalse(page["items"][0]["permissions"]["can_assign"])

    def test_shuhao_has_he_yuting_assignment_permissions_bound_to_number(self):
        shuhao = self.user("FD-026222", "舒豪")
        self.assertTrue(is_video_request_assigner(shuhao))
        self.assertFalse(is_video_request_assigner(self.user("FD-NOT-SHUHAO", "舒豪")))
        with SessionLocal() as db:
            created = video_request_create(VideoRequestCreate(product="通用", description="授权回归"),
                                           db=db, user=self.user("FD-OWNER", "提需同事"))
            shu_page = video_request_list(scope="all", status="all", q="", page=1, page_size=10, db=db, user=shuhao)
            he_page = video_request_list(scope="all", status="all", q="", page=1, page_size=10, db=db,
                                        user=self.user("FD-021068", "何雨庭"))
            self.assertEqual(shu_page["items"][0]["permissions"], he_page["items"][0]["permissions"])
            assigned = video_request_assign(created["id"], VideoRequestAssign(assignee_number="FD-021099", assignee_name="古广妹"),
                                            db=db, user=shuhao)
            self.assertEqual(assigned["status"], "assigned")
            worker = self.user("FD-021099", "古广妹")
            self.assertEqual(video_request_list(scope="assigned", status="all", q="", page=1, page_size=10,
                                               db=db, user=worker)["total"], 1)
            self.assertEqual(video_request_start(created["id"], db=db, user=worker)["status"], "in_production")

    def test_candidates_cover_verified_roster_without_activity_and_exclude_zhao(self):
        expected = {"何雨庭", "张锦玲", "陈广聪", "赖健诚", "何金芝", "马鸿涛", "曾颖", "陈俞婷", "黄小华", "赖鸣悦", "古广妹"}
        with SessionLocal() as db:
            db.query(OaAccessGrant).delete()
            db.add(OaAccessGrant(identifier="FD-117110", user_number="FD-117110", real_name="赵佳乐", center="视频中心", active=True))
            db.add(OaAccessGrant(identifier="FD-OUTSIDE", user_number="FD-OUTSIDE", real_name="其他中心", center="营销中心A", active=True))
            db.add(OaAccessGrant(identifier="FD-021099", user_number="FD-021099", real_name="古广妹", center="AI营销中心", active=True))
            db.add(Asset(object_key="yxb/outside.mp4", filename="outside.mp4", media_type="video", size=10,
                         uploaded_by_number="FD-OUTSIDE", uploaded_by_name="其他中心"))
            db.commit()
            result = video_request_assignees(q="", db=db, user=self.user("FD-026222", "舒豪"))
            self.assertEqual({item["name"] for item in result["items"]}, expected)
            self.assertEqual(result["total"], 11)
            self.assertEqual(video_request_assignees(q="赵佳乐", db=db, user=self.user("FD-026222", "舒豪"))["total"], 0)
            self.assertEqual(video_request_assignees(q="OD-000256", db=db, user=self.user("FD-026222", "舒豪"))["items"][0]["name"], "赖健诚")

    def test_new_active_video_center_member_is_included_and_revoked_member_is_not(self):
        with SessionLocal() as db:
            db.add(OaAccessGrant(identifier="FD-NEW", user_number="FD-NEW", real_name="新制作人", center="视频中心", active=True))
            db.add(OaAccessGrant(identifier="FD-021099", user_number="FD-021099", real_name="古广妹", center="视频中心", active=False))
            db.commit()
            numbers = {item["number"] for item in video_request_assignees(q="", db=db, user=self.user("FD-026222", "舒豪"))["items"]}
            self.assertIn("FD-NEW", numbers)
            self.assertNotIn("FD-021099", numbers)

    def test_invalid_assignment_is_rejected_without_changing_task_or_notifying(self):
        with SessionLocal() as db:
            created = video_request_create(VideoRequestCreate(product="通用", description="分配范围验证"),
                                           db=db, user=self.user("FD-OWNER", "提需同事"))
            events, notices = db.query(VideoRequestEvent).count(), db.query(UserNotification).count()
            for number, name, code in [("FD-117110", "其他人", 409), ("FD-WORKER", "赵佳乐", 409),
                                       ("FD-OUTSIDE", "外部人", 409), ("FD-021099", "张锦玲", 422),
                                       ("", "制作同事", 409)]:
                with self.subTest(number=number, name=name), self.assertRaises(HTTPException) as denied:
                    video_request_assign(created["id"], VideoRequestAssign(assignee_number=number, assignee_name=name),
                                         db=db, user=self.user("FD-026222", "舒豪"))
                self.assertEqual(denied.exception.status_code, code)
            self.assertEqual(db.get(VideoRequest, created["id"]).status, "submitted")
            self.assertEqual(db.query(VideoRequestEvent).count(), events)
            self.assertEqual(db.query(UserNotification).count(), notices)

    def test_name_only_legacy_assignment_resolves_verified_number_and_rejects_same_name_access(self):
        with SessionLocal() as db:
            created = video_request_create(VideoRequestCreate(product="通用", description="历史客户端"),
                                           db=db, user=self.user("FD-OWNER", "提需同事"))
            assigned = video_request_assign(created["id"], VideoRequestAssign(assignee_name="张锦玲"),
                                            db=db, user=self.user("FD-026222", "舒豪"))
            self.assertEqual(assigned["assignee_number"], "FD-026565")
            imposter = self.user("FD-WRONG", "张锦玲")
            self.assertEqual(video_request_list(scope="assigned", status="all", q="", page=1, page_size=10,
                                               db=db, user=imposter)["total"], 0)
            with self.assertRaises(HTTPException):
                video_request_start(created["id"], db=db, user=imposter)

    def test_regular_colleague_cannot_read_assignment_directory(self):
        with SessionLocal() as db, self.assertRaises(HTTPException) as denied:
            video_request_assignees(q="", db=db, user=self.user("FD-WORKER", "制作同事"))
        self.assertEqual(denied.exception.status_code, 403)

    def test_zhao_jiale_has_full_supervisor_access_with_legacy_env(self):
        requester = self.user("FD-REQUESTER", "提需同事")
        zhao_jiale = self.user("FD-ZHAO-JIALE", "赵佳乐")
        worker = self.user("FD-WORKER", "制作同事")
        with patch.dict(os.environ, {"VIDEO_REQUEST_ASSIGNER_NAMES": "何雨庭"}, clear=False), SessionLocal() as db:
            created = video_request_create(
                VideoRequestCreate(product="通用", description="赵佳乐完整权限测试"),
                db=db,
                user=requester,
            )
            request_id = created["id"]
            permissions = user_permissions(zhao_jiale)
            self.assertTrue(permissions["video_request_assigner"])
            self.assertTrue(permissions["video_request_supervisor_viewer"])

            page = video_request_list(
                scope="all", status="all", q="", page=1, page_size=10, db=db, user=zhao_jiale
            )
            self.assertEqual(page["total"], 1)
            item_permissions = page["items"][0]["permissions"]
            self.assertTrue(item_permissions["can_assign"])
            self.assertTrue(item_permissions["can_work"])
            self.assertTrue(item_permissions["can_view_supervisor"])
            self.assertFalse(item_permissions["can_review"])

            assigned = video_request_assign(
                request_id,
                VideoRequestAssign(assignee_number="FD-WORKER", assignee_name="制作同事"),
                db=db,
                user=zhao_jiale,
            )
            self.assertEqual(assigned["status"], "assigned")

    def test_feishu_open_id_delivery_requires_an_explicit_recipient_mapping(self):
        with patch.dict(
            os.environ,
            {
                "FEISHU_RECEIVE_ID_TYPE": "open_id",
                "FEISHU_RECIPIENT_MAP_JSON": '{"FD-KNOWN":"ou_known","已知同事":"ou_known"}',
                "VIDEO_REQUEST_ASSIGNER_FEISHU_ID": "ou_supervisor",
            },
            clear=False,
        ):
            service = FeishuNotificationService()
        self.assertEqual(service._recipient_id("FD-KNOWN", "已知同事"), "ou_known")
        self.assertEqual(service._recipient_id("", "何雨庭"), "ou_supervisor")
        self.assertEqual(service._recipient_id("FD-UNKNOWN", "未知同事"), "")

    def test_feishu_api_error_preserves_platform_code_and_scope_advice(self):
        with patch.dict(
            os.environ,
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret_test",
                "FEISHU_RECEIVE_ID_TYPE": "open_id",
                "FEISHU_RECIPIENT_MAP_JSON": '{"FD-KNOWN":"ou_known"}',
            },
            clear=False,
        ):
            service = FeishuNotificationService()
        token_response = Mock(status_code=200)
        token_response.json.return_value = {
            "code": 0,
            "tenant_access_token": "token",
            "expire": 7200,
        }
        send_response = Mock(status_code=400)
        send_response.json.return_value = {
            "code": 230013,
            "msg": "Bot has no user authority",
        }
        with patch("app.feishu_notification_service.requests.post", side_effect=[token_response, send_response]):
            result = service.send(
                recipient_number="FD-KNOWN",
                recipient_name="已知同事",
                title="测试通知",
                message="无需处理",
            )
        self.assertEqual(result.status, "failed")
        self.assertIn("230013", result.error)
        self.assertIn("可用范围", result.error)


if __name__ == "__main__":
    unittest.main()
