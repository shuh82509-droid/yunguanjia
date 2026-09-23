"""Local HTTP route tests with isolated SQLite and explicit test identity.

These do not claim real OA authentication or external platform acceptance.
"""
import os
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.database import Base, get_db
from app.main import workflow_delivery_receipt, require_user
from app.models import Asset, QianchuanDelivery


class WorkflowReceiptApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add(Asset(id=12, object_key="fixture/video.mp4", filename="演练.mp4", media_type="video"))
            db.add(QianchuanDelivery(id="job", batch_id="batch", asset_id=12, created_by_number="user-a", advertiser_id="a", plan_id="p", status="success", platform_asset_id="v", binding_evidence={"matched_count": 1, "video_id": "v"}, binding_verified_at=datetime(2026, 9, 5)))
            db.commit()
        app = FastAPI()
        app.add_api_route("/api/workflow/receipts/{platform}/{task_id}", workflow_delivery_receipt, methods=["GET"])
        def database():
            with Session(self.engine) as db:
                yield db
        def identity(request: Request):
            number = request.headers.get("x-fixture-user")
            if not number:
                raise HTTPException(401, "fixture identity missing")
            return {"number": number}
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[require_user] = identity
        self.client = TestClient(app)
        self.guard = patch("app.main.require_module_access").start()
        patch("app.main.is_operation_admin", side_effect=lambda u: u["number"] == "admin").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.client.close)
        self.addCleanup(self.engine.dispose)

    def get(self, user="user-a", path="qianchuan/job"):
        return self.client.get("/api/workflow/receipts/" + path, headers={"x-fixture-user": user} if user else {})

    def test_exact_owned_receipt_is_read_only(self):
        result = self.get()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["platform_asset_id"], "v")
        self.assertEqual(self.guard.call_args.args[1], "cloud-manager")
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(QianchuanDelivery)), 1)
            self.assertEqual(db.get(QianchuanDelivery, "job").attempt_count, 0)

    def test_missing_identity_is_denied(self):
        self.assertEqual(self.get(user="").status_code, 401)

    def test_module_denial_is_not_bypassed(self):
        self.guard.side_effect = HTTPException(403, "not granted")
        self.assertEqual(self.get().status_code, 403)

    def test_other_members_record_is_hidden(self):
        self.assertEqual(self.get(user="user-b").status_code, 404)

    def test_admin_can_read_record(self):
        self.assertEqual(self.get(user="admin").status_code, 200)

    def test_unknown_record_or_platform(self):
        self.assertEqual(self.get(path="qianchuan/missing").status_code, 404)
        self.assertEqual(self.get(path="unknown/job").status_code, 404)

    def test_deleted_asset_is_hidden(self):
        with Session(self.engine) as db:
            db.get(Asset, 12).deleted_at = datetime(2026, 9, 5)
            db.commit()
        self.assertEqual(self.get().status_code, 404)

    def test_deleted_delivery_is_hidden(self):
        with Session(self.engine) as db:
            db.get(QianchuanDelivery, "job").deleted_at = datetime(2026, 9, 5)
            db.commit()
        self.assertEqual(self.get().status_code, 404)

    def test_mutations_are_not_exposed(self):
        self.assertEqual(self.client.post("/api/workflow/receipts/qianchuan/job").status_code, 405)
