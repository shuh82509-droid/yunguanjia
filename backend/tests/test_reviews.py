import os
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.database import Base, SessionLocal, engine
from app.main import (
    _require_assets_review_approved,
    _migrate_review_ai_results_to_advisory,
    _review_latest_summaries,
    _review_eligible_reviewers,
    _sync_review_organization_assignments,
    AssetReviewBatchAct,
    AssetReviewBatchActItem,
    AssetReviewBatchSubmit,
    AssetReviewBatchSubmitItem,
    AssetReviewAct,
    AssetReviewSubmit,
    ReviewNamingEvidence,
    ReviewRoleCreate,
    ReviewAiRuleUpdate,
    ReviewAiRulesUpdate,
    ReviewWorkflowUpdate,
    channels_push,
    ensure_asset_schema,
    review_approve,
    review_asset_submit,
    review_assets_batch_submit,
    review_ai_retry,
    review_ai_rules_update,
    review_batch_act,
    review_config,
    review_config_update,
    review_pending_count,
    review_reject,
    review_role_create,
    review_submissions,
    run_review_ai_task,
    update_asset,
    user_permissions,
)
from app.models import Asset, AssetReviewAiResult, AssetReviewDecision, AssetReviewSubmission, ChannelsAccount, ReviewRoleAssignment
from app.review_ai_service import evaluate_redlines
from app.schemas import AssetUpdate, ChannelsPushCreate


class ReviewWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        ensure_asset_schema()

    def setUp(self):
        Base.metadata.drop_all(engine)
        ensure_asset_schema()

    @staticmethod
    def user(number: str, name: str) -> dict:
        return {"number": number, "realName": name, "groupName": "品牌营销部", "status": "normal"}

    def configure_strict_workflow(self, db):
        admin = self.user("FD-026222", "舒豪")
        members = [
            ("member", "FD-1", "组员甲"),
            ("team_lead", "FD-2", "组长乙"),
            ("supervisor", "FD-3", "主管丙"),
        ]
        for role, number, name in members:
            review_role_create(
                ReviewRoleCreate(role_code=role, user_name=name, user_number=number, department="营销中心"),
                db=db,
                user=admin,
            )
        updated = review_config_update(
            ReviewWorkflowUpdate(
                enabled=True,
                naming_enabled=True,
                ai_redline_enabled=True,
                required_roles=["team_lead", "supervisor"],
            ),
            db=db,
            user=admin,
        )
        self.assertTrue(updated["enabled"])
        self.assertTrue(updated["can_enable"])
        return admin

    @staticmethod
    def mark_ai_ready(db, submission_id: str, status: str = "passed"):
        row = db.scalar(
            select(AssetReviewAiResult).where(AssetReviewAiResult.submission_id == submission_id)
        )
        row.status = status
        row.summary = "AI审核测试结果"
        db.commit()
        return row

    @staticmethod
    def quality_scores() -> dict[str, int]:
        return {"hook": 20, "selling_point": 21, "rhythm": 20, "production": 19}

    def test_roles_sequence_and_publish_gate(self):
        with SessionLocal() as db:
            self.configure_strict_workflow(db)
            asset = Asset(
                object_key="yxb/review/strict.mp4",
                filename="待审核视频.mp4",
                media_type="video",
                size=1024,
            )
            account = ChannelsAccount(
                id="channels-1",
                owner_number="FD-1",
                owner_name="组员甲",
                nickname="组员甲的视频号",
                auth_file="/tmp/channels.json",
                status="active",
                authorized_at=datetime.utcnow(),
            )
            db.add_all([asset, account])
            db.commit()

            member = self.user("FD-1", "组员甲")
            created = review_asset_submit(asset.id, AssetReviewSubmit(note="请审核上线版本"), db=db, user=member)
            self.assertEqual(created["current_role"], "team_lead")
            self.assertEqual(created["ai_review"]["status"], "pending")
            self.assertEqual(review_pending_count(db=db, user=self.user("FD-2", "组长乙"))["count"], 1)
            self.assertEqual(user_permissions(member)["reviewer_roles"], ["member"])
            self.assertEqual(review_pending_count(db=db, user=member)["count"], 0)
            self.assertEqual(review_pending_count(db=db, user=self.user("FD-2", "组长乙"))["count"], 1)
            self.assertEqual(review_pending_count(db=db, user=self.user("FD-3", "主管丙"))["count"], 0)

            with self.assertRaises(HTTPException) as blocked:
                channels_push(
                    ChannelsPushCreate(asset_ids=[asset.id], account_ids=[account.id]),
                    db=db,
                    user=member,
                )
            self.assertEqual(blocked.exception.status_code, 409)
            self.assertIn("尚未完成全部审核", blocked.exception.detail)

            with self.assertRaises(HTTPException) as wrong_order:
                review_approve(
                    created["id"],
                    AssetReviewAct(role_code="supervisor", note="抢先审核"),
                    db=db,
                    user=self.user("FD-3", "主管丙"),
                )
            self.assertEqual(wrong_order.exception.status_code, 409)

            for index, (role, number, name) in enumerate([
                ("team_lead", "FD-2", "组长乙"),
                ("supervisor", "FD-3", "主管丙"),
            ]):
                result = review_approve(
                    created["id"],
                    AssetReviewAct(role_code=role, note="审核通过", quality_scores=self.quality_scores()),
                    db=db,
                    user=self.user(number, name),
                )
                self.assertEqual(review_pending_count(db=db, user=self.user(number, name))["count"], 0)
                if index == 0:
                    self.assertEqual(review_pending_count(db=db, user=self.user("FD-3", "主管丙"))["count"], 1)
            self.assertEqual(result["status"], "approved")
            queued = channels_push(
                ChannelsPushCreate(asset_ids=[asset.id], account_ids=[account.id]),
                db=db,
                user=member,
            )
            self.assertEqual(queued["status"], "queued")

    def test_scoped_review_route_blocks_other_marketing_centers(self):
        with SessionLocal() as db:
            admin = self.user("FD-026222", "舒豪")
            for role, number, name, center, group_name in [
                ("member", "FD-A1", "中心A一组组员", "营销中心A", "1组"),
                ("team_lead", "FD-A2", "中心A一组组长", "营销中心A", "1组"),
                ("team_lead", "FD-A3", "中心A二组组长", "营销中心A", "2组"),
                ("team_lead", "FD-B2", "中心B组长", "营销中心B", "1组"),
                ("supervisor", "FD-S1", "中心A负责人", "营销中心A", ""),
            ]:
                review_role_create(
                    ReviewRoleCreate(
                        role_code=role,
                        user_name=name,
                        user_number=number,
                        department="品牌营销部",
                        center=center,
                        group_name=group_name,
                    ),
                    db=db,
                    user=admin,
                )
            review_config_update(
                ReviewWorkflowUpdate(enabled=True, naming_enabled=False, ai_redline_enabled=False),
                db=db,
                user=admin,
            )
            asset = Asset(object_key="yxb/review/scoped.mp4", filename="中心A提审.mp4", media_type="video", size=1)
            db.add(asset)
            db.commit()

            created = review_asset_submit(
                asset.id,
                AssetReviewSubmit(),
                db=db,
                user=self.user("FD-A1", "中心A一组组员"),
            )
            self.assertEqual(created["route_center"], "营销中心A")
            self.assertEqual(created["route_group"], "1组")
            self.assertEqual(created["current_role"], "team_lead")
            self.assertEqual([item["user_name"] for item in created["current_reviewers"]], ["中心A一组组长"])
            self.assertEqual(created["decisions"][1]["candidate_reviewers"][0]["user_name"], "中心A负责人")

            with self.assertRaises(HTTPException) as cross_group:
                review_approve(
                    created["id"],
                    AssetReviewAct(role_code="team_lead", quality_scores=self.quality_scores()),
                    db=db,
                    user=self.user("FD-A3", "中心A二组组长"),
                )
            self.assertEqual(cross_group.exception.status_code, 403)
            self.assertIn("中心A一组组长", cross_group.exception.detail)

            with self.assertRaises(HTTPException) as cross_center:
                review_approve(
                    created["id"],
                    AssetReviewAct(role_code="team_lead", quality_scores=self.quality_scores()),
                    db=db,
                    user=self.user("FD-B2", "中心B组长"),
                )
            self.assertEqual(cross_center.exception.status_code, 403)
            self.assertIn("中心A一组组长", cross_center.exception.detail)

            page = review_submissions(
                q="", reviewer="中心A一组组长", status="all", page=1, page_size=20, db=db, user=admin
            )
            self.assertEqual(page["total"], 1)

    def test_submitter_can_designate_one_reviewer_to_complete_human_review(self):
        with SessionLocal() as db:
            admin = self.user("FD-026222", "舒豪")
            for role, number, name in [
                ("member", "FD-1", "提审人甲"),
                ("team_lead", "FD-2", "组长乙"),
                ("supervisor", "FD-3", "主管丙"),
            ]:
                review_role_create(
                    ReviewRoleCreate(role_code=role, user_name=name, user_number=number),
                    db=db,
                    user=admin,
                )
            config = review_config(db=db, user=admin)
            self.assertFalse(config["enabled"])
            self.assertEqual(
                {item["user_name"] for item in config["eligible_reviewers"]},
                {"组长乙", "主管丙"},
            )
            asset = Asset(object_key="yxb/review/designated.mp4", filename="指定审核人.mp4", media_type="video", size=1)
            db.add(asset)
            db.commit()

            automatic = review_asset_submit(
                asset.id,
                AssetReviewSubmit(),
                db=db,
                user=self.user("FD-1", "提审人甲"),
            )
            self.assertEqual(automatic["assignment_mode"], "organization")
            self.assertEqual(len(automatic["decisions"]), 2)

            created = review_asset_submit(
                asset.id,
                AssetReviewSubmit(
                    assignment_mode="designated",
                    designated_reviewer_number="FD-3",
                    designated_reviewer_name="主管丙",
                ),
                db=db,
                user=self.user("FD-1", "提审人甲"),
            )
            self.assertEqual(created["assignment_mode"], "designated")
            self.assertEqual(created["id"], automatic["id"])
            self.assertEqual(created["designated_reviewer_name"], "主管丙")
            self.assertEqual(created["current_role"], "designated_reviewer")
            self.assertEqual([item["user_name"] for item in created["current_reviewers"]], ["主管丙"])
            self.assertEqual(len(created["decisions"]), 1)
            self.assertEqual(review_pending_count(db=db, user=self.user("FD-3", "主管丙"))["count"], 1)

            with self.assertRaises(HTTPException) as wrong_reviewer:
                review_approve(
                    created["id"],
                    AssetReviewAct(role_code="designated_reviewer", quality_scores=self.quality_scores()),
                    db=db,
                    user=self.user("FD-2", "组长乙"),
                )
            self.assertEqual(wrong_reviewer.exception.status_code, 403)

            approved = review_approve(
                created["id"],
                AssetReviewAct(role_code="designated_reviewer", note="可上线", quality_scores=self.quality_scores()),
                db=db,
                user=self.user("FD-3", "主管丙"),
            )
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["decisions"][0]["reviewer_name"], "主管丙")

    def test_designated_reviewer_must_be_selected_from_active_reviewer_roles(self):
        with SessionLocal() as db:
            asset = Asset(object_key="yxb/review/invalid-reviewer.mp4", filename="无效审核人.mp4", media_type="video", size=1)
            db.add(asset)
            db.commit()
            with self.assertRaises(HTTPException) as invalid:
                review_asset_submit(
                    asset.id,
                    AssetReviewSubmit(
                        assignment_mode="designated",
                        designated_reviewer_number="FD-NOT-REVIEWER",
                        designated_reviewer_name="普通同事",
                    ),
                    db=db,
                    user=self.user("FD-1", "提审人甲"),
                )
            self.assertEqual(invalid.exception.status_code, 409)
            self.assertIn("指定审核人无效", invalid.exception.detail)

    def test_designated_reviewer_can_batch_approve(self):
        with SessionLocal() as db:
            admin = self.user("FD-026222", "舒豪")
            review_role_create(
                ReviewRoleCreate(role_code="supervisor", user_name="主管丙", user_number="FD-3"),
                db=db,
                user=admin,
            )
            assets = [
                Asset(
                    object_key=f"yxb/review/designated-batch-{index}.mp4",
                    filename=f"指定批量审核{index}.mp4",
                    media_type="video",
                    size=1,
                )
                for index in range(3)
            ]
            db.add_all(assets)
            db.commit()
            submissions = [
                review_asset_submit(
                    asset.id,
                    AssetReviewSubmit(
                        assignment_mode="designated",
                        designated_reviewer_number="FD-3",
                        designated_reviewer_name="主管丙",
                    ),
                    db=db,
                    user=admin,
                )
                for asset in assets
            ]

            result = review_batch_act(
                AssetReviewBatchAct(
                    decision="approve",
                    items=[
                        AssetReviewBatchActItem(
                            submission_id=item["id"],
                            role_code="designated_reviewer",
                            note="批量通过",
                            quality_scores=self.quality_scores(),
                        )
                        for item in submissions
                    ],
                ),
                db=db,
                user=self.user("FD-3", "主管丙"),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(len(result["succeeded"]), 3)
            self.assertFalse(result["failed"])
            self.assertTrue(all(item["status"] == "approved" for item in result["succeeded"]))

    def test_whiteboard_roster_sync_uses_group_leads_and_center_owners(self):
        with SessionLocal() as db:
            _sync_review_organization_assignments(db)
            rows = db.scalars(
                select(ReviewRoleAssignment).where(
                    ReviewRoleAssignment.active.is_(True),
                    ReviewRoleAssignment.source == "organization_sync",
                )
            ).all()
            counts = {
                role: sum(1 for row in rows if row.role_code == role)
                for role in ("member", "team_lead", "supervisor")
            }
            self.assertEqual(counts, {"member": 36, "team_lead": 18, "supervisor": 7})
            self.assertEqual(
                {row.user_name for row in rows if row.role_code == "supervisor"},
                {"吴为", "王思怡", "覃琪惠", "彭聪", "曾业高", "练美好", "张鑫露"},
            )
            self.assertEqual(
                {row.user_name for row in rows if row.role_code == "supervisor" and row.center == "AI营销中心"},
                {"吴为", "王思怡"},
            )
            self.assertIn("吴为", {item["user_name"] for item in _review_eligible_reviewers(db)})
            self.assertEqual(
                {row.user_name for row in rows if row.role_code == "team_lead" and row.center == "AI营销中心"},
                {"刘芷含", "陈凯涛", "章佳露"},
            )
            self.assertEqual(
                {item["user_name"] for item in _review_eligible_reviewers(db) if item["center"] == "AI营销中心"},
                {"刘芷含", "陈凯涛", "章佳露", "吴为", "王思怡"},
            )
            self.assertEqual(
                {row.user_name for row in rows if row.role_code == "team_lead" and row.center == "营销中心J"},
                {"唐茹", "施钰荧", "陈静诗"},
            )

    def test_batch_submit_returns_partial_results_without_blocking_successes(self):
        with SessionLocal() as db:
            member = self.user("FD-1", "组员甲")
            asset = Asset(object_key="yxb/review/batch-ok.mp4", filename="批量成功.mp4", media_type="video", size=1)
            db.add(asset)
            db.commit()
            result = review_assets_batch_submit(
                AssetReviewBatchSubmit(items=[
                    AssetReviewBatchSubmitItem(asset_id=asset.id, note="批量提审"),
                    AssetReviewBatchSubmitItem(asset_id=999999, note="不存在"),
                ]),
                db=db,
                user=member,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["succeeded"]), 1)
            self.assertEqual(len(result["failed"]), 1)
            self.assertEqual(result["succeeded"][0]["asset_name"], "批量成功.mp4")

    def test_reject_requires_note_and_resubmission_creates_new_version(self):
        with SessionLocal() as db:
            admin = self.configure_strict_workflow(db)
            asset = Asset(object_key="yxb/review/reject.mp4", filename="修改版.mp4", media_type="video", size=1)
            db.add(asset)
            db.commit()
            first = review_asset_submit(asset.id, AssetReviewSubmit(), db=db, user=admin)
            self.mark_ai_ready(db, first["id"])
            with self.assertRaises(HTTPException) as missing_note:
                review_reject(
                    first["id"], AssetReviewAct(role_code="team_lead", note=""), db=db,
                    user=self.user("FD-2", "组长乙"),
                )
            self.assertEqual(missing_note.exception.status_code, 400)
            rejected = review_reject(
                first["id"], AssetReviewAct(role_code="team_lead", note="字幕颜色需调整"), db=db,
                user=self.user("FD-2", "组长乙"),
            )
            self.assertEqual(rejected["status"], "rejected")
            rejected_notice = review_pending_count(db=db, user=admin)
            self.assertEqual(rejected_notice["rejected_count"], 1)
            rejected_summary = _review_latest_summaries(db, [asset.id])[asset.id]
            self.assertEqual(rejected_summary["status"], "rejected")
            self.assertEqual(rejected_summary["note"], "字幕颜色需调整")
            second = review_asset_submit(asset.id, AssetReviewSubmit(note="已修改字幕"), db=db, user=admin)
            self.assertEqual(second["version"], 2)
            self.assertEqual(second["ai_review"]["status"], "pending")
            self.assertEqual(review_pending_count(db=db, user=admin)["rejected_count"], 0)
            page = review_submissions(q="修改版", status="all", page=1, page_size=20, db=db, user=admin)
            self.assertEqual(page["total"], 2)

    def test_cannot_enable_when_required_role_has_no_member(self):
        with SessionLocal() as db:
            admin = self.user("FD-026222", "舒豪")
            initial = review_config(db=db, user=admin)
            self.assertFalse(initial["enabled"])
            with self.assertRaises(HTTPException) as blocked:
                review_config_update(
                    ReviewWorkflowUpdate(enabled=True, required_roles=["team_lead"]),
                    db=db,
                    user=admin,
                )
            self.assertEqual(blocked.exception.status_code, 409)
            self.assertIn("组长", blocked.exception.detail)

    def test_naming_ai_and_human_gates_are_independent_and_default_off(self):
        with SessionLocal() as db:
            admin = self.user("FD-026222", "舒豪")
            initial = review_config(db=db, user=admin)
            self.assertFalse(initial["enabled"])
            self.assertFalse(initial["naming_standard"]["enabled"])
            self.assertFalse(initial["ai_review_enabled"])

            asset = Asset(
                object_key="yxb/review/independent-gates.mp4",
                filename="未按规范命名.mp4",
                media_type="video",
                library_type="remix",
                size=1,
            )
            db.add(asset)
            db.commit()

            submitted = review_asset_submit(asset.id, AssetReviewSubmit(), db=db, user=admin)
            self.assertEqual(submitted["naming_check"]["status"], "disabled")
            self.assertEqual(submitted["ai_review"]["status"], "disabled")
            self.assertEqual(submitted["current_role"], "")
            _require_assets_review_approved(db, [asset])

            naming_only = review_config_update(
                ReviewWorkflowUpdate(
                    enabled=False,
                    naming_enabled=True,
                    ai_redline_enabled=False,
                    required_roles=["team_lead", "supervisor"],
                ),
                db=db,
                user=admin,
            )
            self.assertTrue(naming_only["naming_standard"]["blocking"])
            self.assertFalse(naming_only["ai_review_enabled"])
            with self.assertRaises(HTTPException) as naming_blocked:
                _require_assets_review_approved(db, [asset])
            self.assertIn("命名规范未通过", naming_blocked.exception.detail)

            ai_only = review_config_update(
                ReviewWorkflowUpdate(
                    enabled=False,
                    naming_enabled=False,
                    ai_redline_enabled=True,
                    required_roles=["team_lead", "supervisor"],
                ),
                db=db,
                user=admin,
            )
            self.assertFalse(ai_only["naming_standard"]["blocking"])
            self.assertTrue(ai_only["ai_review_enabled"])
            self.assertFalse(ai_only["ai_redlines"]["blocking"])
            _require_assets_review_approved(db, [asset])

            all_off = review_config_update(
                ReviewWorkflowUpdate(
                    enabled=False,
                    naming_enabled=False,
                    ai_redline_enabled=False,
                    required_roles=["team_lead", "supervisor"],
                ),
                db=db,
                user=admin,
            )
            self.assertFalse(all_off["enabled"])
            self.assertFalse(all_off["naming_standard"]["enabled"])
            self.assertFalse(all_off["ai_review_enabled"])

    def test_ai_advisory_high_risk_relaxation_and_safe_result(self):
        hard = evaluate_redlines([
            {"time_range": "00:00-00:05", "script_text": "这款产品100%有效，而且零过敏"}
        ])
        self.assertEqual(hard["status"], "warning")
        self.assertEqual(hard["hard_count"], 1)
        self.assertEqual(hard["attention_count"], 1)
        self.assertIn("不自动通过或驳回", hard["summary"])

        warning = evaluate_redlines([
            {"time_range": "00:05-00:09", "scene_description": "画面出现明星同款宣传字样"}
        ])
        self.assertEqual(warning["status"], "warning")
        self.assertEqual(warning["category_counts"]["artist"], 1)

        relaxed = evaluate_redlines([
            {"time_range": "00:09-00:12", "script_text": "一片补水，肌肤水润"}
        ])
        self.assertEqual(relaxed["status"], "warning")
        self.assertEqual(relaxed["relaxed_count"], 1)
        self.assertEqual(relaxed["findings"][0]["policy_effect"], "relaxed")
        self.assertIn("audit-relax-0720", relaxed["findings"][0]["source_url"])

        safe = evaluate_redlines([
            {"time_range": "00:00-00:04", "script_text": "敷上面膜，感受水润肤感"}
        ])
        self.assertEqual(safe["status"], "passed")

    def test_legacy_ai_only_rejection_is_reopened_but_human_rejection_is_preserved(self):
        with SessionLocal() as db:
            first = Asset(object_key="yxb/review/legacy-ai.mp4", filename="历史AI驳回.mp4", media_type="video", size=1)
            second = Asset(object_key="yxb/review/human-reject.mp4", filename="人工驳回.mp4", media_type="video", size=1)
            db.add_all([first, second])
            db.flush()
            ai_only = AssetReviewSubmission(id="legacy-ai-only", asset_id=first.id, version=1, status="rejected", completed_at=datetime.utcnow())
            human_rejected = AssetReviewSubmission(id="human-rejected", asset_id=second.id, version=1, status="rejected", completed_at=datetime.utcnow())
            db.add_all([ai_only, human_rejected])
            db.flush()
            db.add_all([
                AssetReviewAiResult(submission_id=ai_only.id, status="rejected", summary="历史AI自动驳回"),
                AssetReviewAiResult(submission_id=human_rejected.id, status="rejected", summary="历史AI自动驳回"),
                AssetReviewDecision(submission_id=human_rejected.id, role_code="team_lead", status="rejected", reviewer_name="组长乙"),
            ])
            db.commit()

            _migrate_review_ai_results_to_advisory(db, datetime.utcnow())
            db.commit()
            db.expire_all()

            self.assertEqual(db.get(AssetReviewSubmission, ai_only.id).status, "pending")
            self.assertIsNone(db.get(AssetReviewSubmission, ai_only.id).completed_at)
            self.assertEqual(db.get(AssetReviewSubmission, human_rejected.id).status, "rejected")
            statuses = db.scalars(select(AssetReviewAiResult.status).order_by(AssetReviewAiResult.id)).all()
            self.assertEqual(statuses, ["warning", "warning"])

    def test_ai_redline_rules_are_visible_editable_versioned_and_enforced(self):
        with SessionLocal() as db:
            admin = self.configure_strict_workflow(db)
            initial = review_config(db=db, user=admin)
            self.assertEqual(initial["ai_redlines"]["version"], "wis-redline-v1")
            self.assertGreaterEqual(initial["ai_redlines"]["enabled_count"], 10)
            self.assertEqual(
                {rule["category"] for rule in initial["ai_redlines"]["rules"]},
                {"platform", "internal", "artist", "relaxation"},
            )
            self.assertEqual(initial["ai_redlines"]["mode"], "advisory")
            self.assertFalse(initial["ai_redlines"]["blocking"])
            self.assertEqual(initial["ai_redlines"]["policy_source"]["case_count"], 273)

            updated = review_ai_rules_update(
                ReviewAiRulesUpdate(rules=[ReviewAiRuleUpdate(
                    code="custom-test-block",
                    category="internal",
                    severity="hard",
                    title="测试自定义红线",
                    pattern="必须拦截的测试词",
                    enabled=True,
                )]),
                db=db,
                user=admin,
            )
            self.assertEqual(updated["ai_redlines"]["version"], "wis-redline-v2")
            self.assertEqual(updated["ai_redlines"]["enabled_count"], 1)

            with self.assertRaises(HTTPException) as forbidden:
                review_ai_rules_update(
                    ReviewAiRulesUpdate(rules=[ReviewAiRuleUpdate(
                        code="custom-test-block",
                        category="internal",
                        severity="hard",
                        title="无权限修改",
                        pattern="无权限",
                        enabled=True,
                    )]),
                    db=db,
                    user=self.user("FD-1", "组员甲"),
                )
            self.assertEqual(forbidden.exception.status_code, 403)

            asset = Asset(object_key="yxb/review/custom-rule.mp4", filename="自定义红线.mp4", media_type="video", size=1)
            db.add(asset)
            db.commit()
            created = review_asset_submit(asset.id, AssetReviewSubmit(), db=db, user=admin)
            ai_row = db.scalar(select(AssetReviewAiResult).where(AssetReviewAiResult.submission_id == created["id"]))
            ai_row.status = "processing"
            db.commit()
            with patch("app.main.review_ai_service.analyze", return_value={
                "task_id": "custom-rule-task",
                "results": [{"time_range": "00:00-00:04", "script_text": "这里出现必须拦截的测试词"}],
            }):
                run_review_ai_task(ai_row.id)
            db.expire_all()
            result = db.get(AssetReviewAiResult, ai_row.id)
            self.assertEqual(result.status, "warning")
            self.assertEqual(result.rule_version, "wis-redline-v2")
            self.assertEqual(db.get(AssetReviewSubmission, created["id"]).status, "pending")

    def test_ai_job_is_advisory_and_error_never_blocks_human_review(self):
        with SessionLocal() as db:
            admin = self.configure_strict_workflow(db)
            asset = Asset(object_key="yxb/review/ai.mp4", filename="AI红线.mp4", media_type="video", size=1)
            db.add(asset)
            db.commit()
            created = review_asset_submit(asset.id, AssetReviewSubmit(), db=db, user=admin)
            ai_row = db.scalar(select(AssetReviewAiResult).where(AssetReviewAiResult.submission_id == created["id"]))
            ai_row.status = "processing"
            db.commit()
            with patch("app.main.review_ai_service.analyze", return_value={
                "task_id": "task-test",
                "results": [{"time_range": "00:00-00:04", "script_text": "加微信私下交易"}],
            }):
                run_review_ai_task(ai_row.id)
            db.expire_all()
            self.assertEqual(db.get(AssetReviewSubmission, created["id"]).status, "pending")
            self.assertEqual(db.get(AssetReviewAiResult, ai_row.id).status, "warning")

            second_asset = Asset(object_key="yxb/review/ai-error.mp4", filename="AI失败.mp4", media_type="video", size=1)
            db.add(second_asset)
            db.commit()
            second = review_asset_submit(second_asset.id, AssetReviewSubmit(), db=db, user=admin)
            error_row = db.scalar(select(AssetReviewAiResult).where(AssetReviewAiResult.submission_id == second["id"]))
            error_row.status = "error"
            error_row.error_message = "临时超时"
            db.commit()
            reviewed = review_approve(
                second["id"],
                AssetReviewAct(role_code="team_lead", quality_scores=self.quality_scores()),
                db=db,
                user=self.user("FD-2", "组长乙"),
            )
            self.assertEqual(reviewed["current_role"], "supervisor")
            retried = review_ai_retry(second["id"], db=db, user=admin)
            self.assertEqual(retried["ai_review"]["status"], "pending")
            self.assertEqual(retried["status"], "pending")

    def test_remix_naming_is_required_and_rename_invalidates_approval(self):
        with SessionLocal() as db:
            admin = self.configure_strict_workflow(db)
            asset = Asset(
                object_key="yxb/review/naming-pass.mp4",
                filename="0829-哈哈鱼摆-水润版.mp4",
                media_type="video",
                library_type="remix",
                size=1,
            )
            missing = Asset(
                object_key="yxb/review/naming-fail.mp4",
                filename="0829-水润版.mp4",
                media_type="video",
                library_type="remix",
                size=1,
            )
            db.add_all([asset, missing])
            db.commit()
            evidence = ReviewNamingEvidence(
                category="face",
                material_name="哈哈鱼摆.mp4",
                usage="complete",
                position="middle",
            )

            with self.assertRaises(HTTPException) as incomplete:
                review_asset_submit(missing.id, AssetReviewSubmit(), db=db, user=admin)
            self.assertEqual(incomplete.exception.status_code, 409)
            self.assertIn("补充混剪使用", incomplete.exception.detail)

            with self.assertRaises(HTTPException) as wrong_name:
                review_asset_submit(
                    missing.id,
                    AssetReviewSubmit(naming_evidence=[evidence]),
                    db=db,
                    user=admin,
                )
            self.assertEqual(wrong_name.exception.status_code, 409)
            self.assertIn("缺少：哈哈鱼摆.mp4", wrong_name.exception.detail)

            created = review_asset_submit(
                asset.id,
                AssetReviewSubmit(naming_evidence=[evidence]),
                db=db,
                user=admin,
            )
            self.assertEqual(created["naming_check"]["status"], "passed")
            self.assertEqual(created["naming_check"]["required_names"], ["哈哈鱼摆.mp4"])
            self.mark_ai_ready(db, created["id"])
            for role, number, name in [
                ("team_lead", "FD-2", "组长乙"),
                ("supervisor", "FD-3", "主管丙"),
            ]:
                review_approve(
                    created["id"],
                    AssetReviewAct(role_code=role, quality_scores=self.quality_scores()),
                    db=db,
                    user=self.user(number, name),
                )
            _require_assets_review_approved(db, [asset])

            update_asset(
                asset.id,
                AssetUpdate(filename="0829-水润改名版.mp4"),
                db=db,
                user=admin,
            )
            db.refresh(asset)
            with self.assertRaises(HTTPException) as renamed:
                _require_assets_review_approved(db, [asset])
            self.assertIn("命名规范未通过", renamed.exception.detail)
            self.assertEqual(_review_latest_summaries(db, [asset.id])[asset.id]["status"], "rejected")

    def test_batch_review_and_reviewer_filter(self):
        with SessionLocal() as db:
            admin = self.configure_strict_workflow(db)
            assets = [
                Asset(object_key=f"yxb/review/batch-{index}.mp4", filename=f"批量审核{index}.mp4", media_type="video", size=1)
                for index in range(2)
            ]
            db.add_all(assets)
            db.commit()
            submissions = [
                review_asset_submit(asset.id, AssetReviewSubmit(), db=db, user=admin)
                for asset in assets
            ]
            for item in submissions:
                self.mark_ai_ready(db, item["id"])

            result = review_batch_act(
                AssetReviewBatchAct(
                    decision="approve",
                    items=[
                        AssetReviewBatchActItem(
                            submission_id=item["id"],
                            role_code="team_lead",
                            note="批量通过",
                            quality_scores=self.quality_scores(),
                        )
                        for item in submissions
                    ],
                ),
                db=db,
                user=self.user("FD-2", "组长乙"),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["succeeded"]), 2)
            filtered = review_submissions(
                q="",
                reviewer="组长乙",
                status="all",
                page=1,
                page_size=20,
                db=db,
                user=admin,
            )
            self.assertEqual(filtered["total"], 2)

    def test_legacy_naming_snapshot_is_normalized_for_review_center(self):
        with SessionLocal() as db:
            admin = self.configure_strict_workflow(db)
            asset = Asset(
                object_key="yxb/review/legacy-naming.mp4",
                filename="历史审核记录.mp4",
                media_type="video",
                size=1,
            )
            db.add(asset)
            db.commit()
            created = review_asset_submit(asset.id, AssetReviewSubmit(), db=db, user=admin)
            submission = db.get(AssetReviewSubmission, created["id"])
            submission.naming_check = {"status": "passed", "message": "历史版本已通过"}
            db.commit()

            result = review_submissions(
                q="",
                reviewer="",
                status="all",
                page=1,
                page_size=20,
                db=db,
                user=admin,
            )
            row = next(item for item in result["items"] if item["id"] == created["id"])
            self.assertEqual(row["naming_check"]["status"], "passed")
            self.assertEqual(row["naming_check"]["required_names"], [])
            self.assertEqual(row["naming_check"]["missing_names"], [])


if __name__ == "__main__":
    unittest.main()
