import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from fastapi import HTTPException
from app.database import Base, SessionLocal, engine
from app.main import _publish_private_asset
from app.models import Asset
from app.private_assets import PrivateAsset


class PrivateSharingTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def test_explicit_share_retry_reuses_one_public_copy_and_keeps_original(self):
        with tempfile.TemporaryDirectory(prefix='wis-share-test-') as directory, SessionLocal() as db:
            file = Path(directory) / 'private.media'
            file.write_bytes(b'fixture')
            private = PrivateAsset(id='00000000-0000-4000-8000-000000000001', owner_number='A', owner_name='本人',
                                   filename='私人.mp4', content_type='video/mp4', category='黑晶面膜', folder_name='自有',
                                   sha256=hashlib.sha256(b'fixture').hexdigest(), size=7, status='ready')
            db.add(private); db.commit()
            with patch('app.main.oss_service.upload_private_share') as upload, patch('app.main._invalidate_catalog_cache'):
                first = _publish_private_asset(db, private, file, {'number': 'A', 'name': '本人'})
                second = _publish_private_asset(db, private, file, {'number': 'A', 'name': '本人'})
            self.assertEqual(first, second)
            self.assertEqual(upload.call_count, 1)
            public = db.get(Asset, first['shared_asset_id'])
            self.assertEqual(public.uploaded_by_number, 'A')
            self.assertEqual(public.tags, ['主动共享'])
            self.assertEqual(file.read_bytes(), b'fixture')
            self.assertIn('/private-shared/', public.object_key)

    def test_storage_failure_does_not_claim_shared_or_enter_public_catalog(self):
        with SessionLocal() as db:
            private = PrivateAsset(id='00000000-0000-4000-8000-000000000002', owner_number='A', filename='私人.mp4',
                                   content_type='video/mp4', sha256='a'*64, size=7, status='ready')
            db.add(private); db.commit()
            with patch('app.main.oss_service.upload_private_share', side_effect=RuntimeError('storage unavailable')):
                with self.assertRaises(HTTPException):
                    _publish_private_asset(db, private, Path('unused'), {'number': 'A'})
            self.assertIsNone(private.shared_asset_id)
            self.assertEqual(db.query(Asset).count(), 0)
