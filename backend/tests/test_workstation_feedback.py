import os
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from fastapi import HTTPException
from app.database import Base, SessionLocal, engine
from app.main import workstation_return_feedback
from app.models import Asset, QianchuanDelivery, WorkstationReturn


class WorkstationFeedbackTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            db.add_all([
                Asset(id=1, object_key='feedback/1.mp4', filename='自动混剪.mp4', media_type='video'),
                Asset(id=2, object_key='feedback/2.mp4', filename='unrelated.mp4', media_type='video'),
                WorkstationReturn(idempotency_key='return-1', object_key='feedback/1.mp4', asset_id=1, filename='自动混剪.mp4', file_size=100, sha256='a' * 64, status='completed', provenance={'maker_id': 'owner', 'render_id': 'r', 'variant_id': 'v'}),
            ])
            db.commit()

    def task(self, task_id, asset_id=1, deleted=False):
        return QianchuanDelivery(id=task_id, batch_id='batch', asset_id=asset_id,
            created_by_number='cloud-operator', created_by_name='Cloud operator',
            advertiser_id='account', advertiser_name='Account', plan_id='plan', plan_name='Plan',
            idempotency_key=task_id, platform_asset_id='platform-' + task_id,
            status='success', binding_verified_at=datetime.utcnow(),
            deleted_at=datetime.utcnow() if deleted else None)

    def read(self, db, actor='owner', render='r', variant='v'):
        return workstation_return_feedback('return-1', actor, render, variant, db, {})

    def test_manual_cloud_push_is_joined_by_returned_asset_not_operator_or_guess(self):
        with SessionLocal() as db:
            db.add_all([self.task('manual'), self.task('unrelated', 2), self.task('deleted', deleted=True)])
            db.commit()
            with patch('app.main._task_daily_summary', return_value={'status': 'pending', 'metrics': {}}):
                result = self.read(db)
            self.assertEqual([task['id'] for task in result['items']], ['manual'])
            self.assertFalse(result['items'][0]['can_manage'])
            self.assertEqual(result['items'][0]['metrics'], {})
            self.assertEqual(result['items'][0]['metrics_data_status'], 'pending')
            self.assertIsNotNone(result['items'][0]['binding_verified_at'])

    def test_owner_and_both_provenance_ids_are_required(self):
        with SessionLocal() as db:
            for actor, render, variant in [('other', 'r', 'v'), ('owner', 'wrong', 'v'), ('owner', 'r', 'wrong'), ('', 'r', 'v')]:
                with self.assertRaises(HTTPException) as error:
                    self.read(db, actor, render, variant)
                self.assertEqual(error.exception.status_code, 404)

    def test_deleted_asset_does_not_leak_receipts(self):
        with SessionLocal() as db:
            db.add(self.task('manual'))
            db.get(Asset, 1).deleted_at = datetime.utcnow()
            db.commit()
            result = self.read(db)
            self.assertFalse(result['asset_available'])
            self.assertEqual(result['items'], [])

    def test_report_read_is_bounded_and_partial_is_explicit(self):
        with SessionLocal() as db:
            db.add_all([self.task('receipt-' + str(i)) for i in range(102)])
            db.commit()
            with patch('app.main._task_daily_summary', return_value={'status': 'pending'}) as summary:
                result = self.read(db)
            self.assertEqual(len(result['items']), 100)
            self.assertEqual(summary.call_count, 100)
            self.assertFalse(result['complete'])


if __name__ == '__main__':
    unittest.main()
