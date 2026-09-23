import os
import unittest
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app.ai_insight_service import AiInsightService
from app.database import Base, SessionLocal, engine
from app.models import Asset, ModuleAccessGrant, OaAccessGrant, OperationLog, QianchuanDelivery


class AiInsightServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(engine)

    def setUp(self):
        with SessionLocal() as db:
            for model in (OperationLog, QianchuanDelivery, ModuleAccessGrant, OaAccessGrant, Asset):
                db.query(model).delete()
            asset = Asset(
                id=1,
                object_key="tests/insight.mp4",
                filename="insight.mp4",
                media_type="video",
                size=1024,
            )
            db.add(asset)
            now = datetime.utcnow()
            db.add_all([
                OperationLog(
                    module="剪映互传",
                    action="presign",
                    method="POST",
                    path="/api/jianying/device/uploads/presign",
                    result="failed" if index < 5 else "success",
                    status_code=422 if index < 5 else 200,
                    created_at=now,
                )
                for index in range(10)
            ])
            db.add(QianchuanDelivery(
                id="qc-insight",
                batch_id="batch-insight",
                asset_id=1,
                created_by_number="FD-TEST",
                advertiser_id="1001",
                status="pending",
                metrics_link_status="pending",
                created_at=now,
                updated_at=now - timedelta(hours=30),
            ))
            db.add_all([
                OaAccessGrant(
                    identifier="number:FD-A",
                    identifier_type="number",
                    real_name="成员A",
                    user_number="FD-A",
                    department="品牌营销部",
                    center="WIS品牌中心",
                    active=True,
                ),
                OaAccessGrant(
                    identifier="number:FD-B",
                    identifier_type="number",
                    real_name="成员B",
                    user_number="FD-B",
                    department="",
                    center="",
                    active=True,
                ),
                ModuleAccessGrant(
                    identifier="number:FD-B",
                    identifier_type="number",
                    real_name="成员B",
                    user_number="FD-B",
                    access_mode="selected",
                    modules=["material-workbench"],
                ),
            ])
            db.commit()

    def test_refresh_builds_evidence_without_collapsing_pending_status(self):
        service = AiInsightService()
        snapshot = service.refresh()

        self.assertEqual(snapshot["operations_24h"]["failed"], 5)
        self.assertEqual(snapshot["delivery"]["qianchuan"]["status"]["pending"], 1)
        self.assertEqual(snapshot["delivery"]["qianchuan"]["metrics"]["pending"], 1)
        self.assertEqual(snapshot["permissions"]["missing_department"], 1)
        self.assertEqual(snapshot["permissions"]["missing_center"], 1)
        self.assertTrue(any(item["key"] == "qianchuan-metrics-stale" for item in snapshot["insights"]))

    def test_view_filters_operational_and_permission_evidence(self):
        service = AiInsightService()
        service.refresh()

        limited = service.view(["material-workbench"], can_manage_permissions=False)
        admin = service.view(["cloud-manager"], can_manage_permissions=True)

        self.assertNotIn("assets", limited["snapshot"])
        self.assertNotIn("permissions", limited["snapshot"])
        self.assertFalse(any(item["module_key"] == "cloud-manager" for item in limited["insights"]))
        self.assertIn("assets", admin["snapshot"])
        self.assertIn("permissions", admin["snapshot"])
        self.assertTrue(any(item["key"] == "permission-profile-completeness" for item in admin["insights"]))

    def test_module_availability_is_not_inferred_from_static_catalog(self):
        snapshot=AiInsightService().refresh()
        self.assertIsNone(snapshot['modules']['online'])
        self.assertEqual(snapshot['modules']['state'],'unverified')
        self.assertFalse(any(item['key']=='module-integration-progress' for item in snapshot['insights']))

    def test_old_scan_is_not_reported_healthy(self):
        service=AiInsightService();service.refresh()
        service._snapshot['scanned_at']=(datetime.utcnow()-timedelta(hours=1)).isoformat()+'Z'
        result=service.view(['material-workbench'])
        self.assertEqual(result['status'],'stale')
        self.assertTrue(result['snapshot']['stale'])

    def test_no_operations_does_not_mean_zero_failure_rate(self):
        with SessionLocal() as db:
            db.query(OperationLog).delete();db.commit()
        snapshot=AiInsightService().refresh()
        self.assertEqual(snapshot['operations_24h']['total'],0)
        self.assertIsNone(snapshot['operations_24h']['failure_rate'])


if __name__ == "__main__":
    unittest.main()
