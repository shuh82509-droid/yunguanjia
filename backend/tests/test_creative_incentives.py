import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.creative_incentive_service import compose_share_text, sync_directions
from app.database import Base
from app.main import creative_incentive_overview
from app.models import CreativeIncentiveDirection, CreativeIncentiveMilestone


class CreativeIncentiveTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    @staticmethod
    def direction(**updates):
        values = {
            "id": "direction-1",
            "direction_name": "95岁金句＋创新明星形象",
            "material_id": "material-1",
            "material_name": "原创素材.mp4",
            "creator_number": "FD-TEST",
            "creator_name": "测试同事",
            "department": "品牌营销部",
            "center": "营销中心B",
            "online_date": "2026-08-27",
            "originality_status": "confirmed",
            "originality_note": "从0到1的新开头与人物结构",
            "copy_judgement": "用第一人称金句圈定30岁以上人群",
            "visual_judgement": "前三秒创新人物形象建立注意力",
            "voice_judgement": "温柔可信的女性声音",
            "remix_plan": "替换明星与产品镜继续裂变",
            "reference_url": "https://example.com/material",
            "created_by_number": "FD-TEST",
            "created_by_name": "测试同事",
        }
        values.update(updates)
        return CreativeIncentiveDirection(**values)

    def test_confirmed_direction_generates_one_point_per_20k_and_is_idempotent(self):
        direction = self.direction()
        self.db.add(direction)
        self.db.commit()
        snapshot = {
            "status": "ready",
            "topMaterials": [{
                "materialId": "material-1",
                "gmvYuan": 45_000,
                "costYuan": 12_000,
                "roi": 3.75,
                "sourceCutoffAt": "2026-08-27 15:00:00",
                "sourceUpdatedAt": "2026-08-27 15:03:00",
            }],
        }

        first = sync_directions(self.db, [direction], snapshot)
        second = sync_directions(self.db, [direction], snapshot)
        milestones = self.db.scalars(
            select(CreativeIncentiveMilestone).order_by(CreativeIncentiveMilestone.point_number)
        ).all()

        self.assertEqual(first, {"matched": 1, "unmatched": 0, "generated": 2})
        self.assertEqual(second["generated"], 0)
        self.assertEqual([item.threshold_gmv_yuan for item in milestones], [20_000, 40_000])
        self.assertEqual(milestones[0].share_type, "full")
        self.assertIn("有效点判断", milestones[0].share_text)
        self.assertEqual(milestones[1].share_type, "update")
        self.assertIn("累计：2分", milestones[1].share_text)

    def test_unmatched_material_remains_unknown_instead_of_zero(self):
        direction = self.direction()
        self.db.add(direction)
        self.db.commit()

        result = sync_directions(self.db, [direction], {"status": "ready", "topMaterials": []})

        self.assertEqual(result, {"matched": 0, "unmatched": 1, "generated": 0})
        self.assertEqual(direction.metric_status, "unmatched")
        self.assertIsNone(direction.latest_gmv_yuan)
        self.assertIsNone(direction.latest_cost_yuan)
        self.assertEqual(self.db.scalar(select(CreativeIncentiveMilestone.id)), None)

    def test_unconfirmed_direction_never_generates_points(self):
        direction = self.direction(originality_status="pending")
        self.db.add(direction)
        self.db.commit()

        sync_directions(self.db, [direction], {
            "status": "ready",
            "topMaterials": [{"materialId": "material-1", "gmvYuan": 88_000, "costYuan": 20_000, "roi": 4.4}],
        })

        self.assertEqual(direction.latest_gmv_yuan, 88_000)
        self.assertIsNone(self.db.scalar(select(CreativeIncentiveMilestone.id)))

    def test_full_and_followup_share_formats_are_distinct(self):
        direction = self.direction(
            latest_gmv_yuan=60_000,
            latest_cost_yuan=20_000,
            latest_roi=3,
            source_cutoff_at="2026-08-27 16:00:00",
        )

        first = compose_share_text(direction, 1)
        followup = compose_share_text(direction, 3)

        self.assertIn("分享时间", first)
        self.assertIn("参考素材", first)
        self.assertNotIn("有效点判断", followup)
        self.assertIn("累计：3分", followup)


class CreativeIncentiveEndpointTests(unittest.TestCase):
    @patch("app.main._creative_incentive_overview_payload")
    @patch("app.main.require_module_access")
    def test_overview_requires_material_incentive_module(self, require_access: MagicMock, payload: MagicMock):
        db = MagicMock()
        user = {"number": "FD-TEST", "status": "normal"}
        payload.return_value = {"directions": []}

        result = creative_incentive_overview(db=db, user=user)

        require_access.assert_called_once_with(user, "material-incentive", db)
        self.assertEqual(result, {"directions": []})


if __name__ == "__main__":
    unittest.main()
