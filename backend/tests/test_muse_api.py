from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_user
from app.database import get_db
from app.muse_api import muse_cutter, router


class FakeSession:
    def __init__(self, asset):
        self.asset = asset

    def get(self, _model, asset_id):
        return self.asset if self.asset and self.asset.id == asset_id else None

    def scalar(self, _statement):
        return None


def build_client(asset):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: FakeSession(asset)
    app.dependency_overrides[require_user] = lambda: {"number": "FD-TEST", "name": "测试用户"}
    return TestClient(app)


class MuseApiTests(TestCase):
    def setUp(self):
        self.asset = SimpleNamespace(
            id=18,
            filename="眼妆教程.mp4",
            object_key="yxb/uploads/test/eye.mp4",
            media_type="video",
            deleted_at=None,
            purged_at=None,
            reference_url="https://www.douyin.com/video/7679371966209363209",
        )

    @patch("app.muse_api.oss_service.url_for", return_value="https://oss.example/eye.mp4")
    @patch.object(muse_cutter, "submit", return_value={"task_id": "task-18", "status": "pending"})
    @patch.object(muse_cutter, "select", return_value=[])
    def test_submit_returns_pollable_task(self, _select, _submit, _url):
        response = build_client(self.asset).post("/api/muse/analyses", json={"asset_id": 18})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_id"], "task-18")
        self.assertEqual(response.json()["status"], "pending")

    @patch("app.muse_api.oss_service.url_for", return_value="https://oss.example/eye.mp4")
    @patch.object(muse_cutter, "select", return_value=[{
        "index": 1,
        "time_range": "00:00-00:03",
        "scene_description": "眼妆完成效果特写",
        "script_text": "先看完成后的眼妆效果",
        "scene_text": "新手眼妆",
        "camera_angle": "正面",
        "shot_size": "特写",
        "camera_movement": "固定",
        "expression_style": "自然",
    }])
    def test_reuses_existing_result_and_builds_transcript(self, _select, _url):
        response = build_client(self.asset).post("/api/muse/analyses", json={"asset_id": 18})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.assertTrue(response.json()["reused"])
        self.assertIn("完成后的眼妆", response.json()["transcript"])

    def test_rejects_non_video_asset(self):
        self.asset.media_type = "image"
        response = build_client(self.asset).post("/api/muse/analyses", json={"asset_id": 18})
        self.assertEqual(response.status_code, 400)
        self.assertIn("仅支持分析视频", response.json()["detail"])
