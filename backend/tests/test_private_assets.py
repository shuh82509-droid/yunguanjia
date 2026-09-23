import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from app.database import Base, SessionLocal, engine
from app.models import Asset
from app.private_assets import PrivateAsset, build_private_router, media_path


class PrivateAssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='wis-private-vault-')
        self.env = patch.dict(os.environ, {'PRIVATE_ASSET_DATA_DIR': self.directory.name,
            'PRIVATE_ASSETS_ENABLED': 'true', 'PRIVATE_ASSET_ALLOWED_USERS': 'A,B'})
        self.env.start()
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.publisher = unittest.mock.Mock(return_value={'shared_asset_id': 123, 'private_original_preserved': True})
        def user(request: Request):
            if not request.headers.get('x-test-owner'):
                raise HTTPException(401, '未登录')
            return {'number': request.headers['x-test-owner']}
        def service(request: Request):
            if request.headers.get('x-test-service') != 'service':
                raise HTTPException(401, '服务未授权')
            return {}
        app = FastAPI()
        app.include_router(build_private_router(user, service, self.publisher))
        self.client = TestClient(app)
        self.validate = patch('app.private_assets.validate_media')
        self.validate.start()
        self.data = b'private video fixture'

    def tearDown(self):
        self.client.close()
        self.validate.stop()
        self.env.stop()
        self.directory.cleanup()

    def call(self, method, url, owner='A', **kwargs):
        return self.client.request(method, url, headers={'x-test-owner': owner} if owner else {}, **kwargs)

    def start(self, owner='A', data=None):
        data = self.data if data is None else data
        response = self.call('POST', '/api/private-assets/uploads', owner, json={
            'filename': '测试素材.mp4', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'category': '黑晶面膜', 'folder_name': '小组/产品镜',
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['asset']['id']

    def ready(self, owner='A'):
        asset_id = self.start(owner)
        self.assertEqual(self.call('PUT', f'/api/private-assets/{asset_id}/upload?offset=0', owner, content=self.data).status_code, 200)
        result = self.call('POST', f'/api/private-assets/{asset_id}/complete', owner)
        self.assertEqual(result.status_code, 200, result.text)
        return asset_id

    def test_private_bytes_never_enter_shared_asset_table_or_public_storage(self):
        asset_id = self.ready()
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Asset)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(PrivateAsset)), 1)
        self.publisher.assert_not_called()
        self.assertEqual(media_path(asset_id).read_bytes(), self.data)
        result = self.call('GET', '/api/private-assets?q=测试').json()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['folders'], ['小组/产品镜'])
        self.assertNotIn('owner_number', result['items'][0])
        self.assertNotIn(self.directory.name, str(result))

    def test_other_user_and_anonymous_cannot_list_search_preview_download_or_mutate(self):
        asset_id = self.ready()
        self.assertEqual(self.call('GET', '/api/private-assets?q=测试', 'B').json()['total'], 0)
        self.assertEqual(self.call('GET', '/api/private-assets', 'B').json()['folders'], [])
        for method, path, payload in [
            ('GET', '/media', {}), ('GET', '/media?download=true', {}), ('GET', '/upload', {}),
            ('PATCH', '', {'json': {'filename': 'other.mp4'}}), ('DELETE', '', {}),
            ('POST', '/restore', {}), ('POST', '/complete', {}), ('POST', '/share', {'json': {'confirmed': True}}),
        ]:
            for owner, expected in [('B', 404), ('', 401)]:
                response = self.call(method, f'/api/private-assets/{asset_id}{path}', owner, **payload)
                self.assertEqual(response.status_code, expected, response.text)
        self.publisher.assert_not_called()

    def test_private_media_uses_owner_auth_range_and_no_store(self):
        asset_id = self.ready()
        response = self.client.get(f'/api/private-assets/{asset_id}/media', headers={'x-test-owner': 'A', 'Range': 'bytes=1-4'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, self.data[1:5])
        self.assertEqual(response.headers['cache-control'], 'private, no-store')

    def test_resume_and_repeated_part_are_idempotent_and_no_overwrite(self):
        asset_id = self.start()
        url = f'/api/private-assets/{asset_id}/upload'
        for _ in range(2):
            self.assertEqual(self.call('PUT', url + '?offset=0', content=self.data[:5]).status_code, 200)
        self.assertEqual(self.call('GET', url).json()['offset'], 5)
        self.assertEqual(self.start(), asset_id)
        self.assertEqual(self.call('PUT', url + '?offset=0', content=b'wrong').status_code, 409)
        self.assertEqual(self.call('POST', f'/api/private-assets/{asset_id}/complete').status_code, 409)
        self.assertEqual(self.call('PUT', url + '?offset=5', content=self.data[5:]).status_code, 200)
        self.assertEqual(self.call('POST', f'/api/private-assets/{asset_id}/complete').status_code, 200)
        self.assertEqual(self.call('PUT', url + '?offset=0', content=self.data).status_code, 409)
        self.assertEqual(media_path(asset_id).read_bytes(), self.data)

    def test_same_owner_dedup_does_not_reveal_another_owner_upload(self):
        a = self.ready('A')
        self.assertEqual(self.start('A'), a)
        b = self.start('B')
        self.assertNotEqual(a, b)
        self.assertEqual(self.call('GET', f'/api/private-assets/{b}/upload', 'B').json()['offset'], 0)

    def test_delete_is_recoverable_and_media_immediately_unavailable(self):
        asset_id = self.ready()
        self.assertTrue(self.call('DELETE', f'/api/private-assets/{asset_id}').json()['recoverable'])
        self.assertEqual(self.call('GET', f'/api/private-assets/{asset_id}/media').status_code, 404)
        self.assertEqual(self.call('GET', '/api/private-assets?trash=true').json()['total'], 1)
        self.assertTrue(media_path(asset_id).exists())
        self.assertEqual(self.call('POST', f'/api/private-assets/{asset_id}/restore').status_code, 200)
        self.assertEqual(self.call('GET', f'/api/private-assets/{asset_id}/media').content, self.data)

    def test_share_requires_explicit_confirmation_and_keeps_private_original(self):
        asset_id = self.ready()
        self.assertEqual(self.call('POST', f'/api/private-assets/{asset_id}/share', json={'confirmed': False}).status_code, 400)
        self.publisher.assert_not_called()
        self.assertEqual(self.call('POST', f'/api/private-assets/{asset_id}/share', json={'confirmed': True}).status_code, 200)
        self.publisher.assert_called_once()
        self.assertEqual(media_path(asset_id).read_bytes(), self.data)

    def test_service_access_requires_service_auth_and_matching_owner(self):
        asset_id = self.ready()
        self.assertEqual(self.client.get(f'/api/workstation/private-assets/{asset_id}/media?actor_number=A').status_code, 401)
        headers = {'x-test-service': 'service'}
        self.assertEqual(self.client.get(f'/api/workstation/private-assets/{asset_id}/media?actor_number=B', headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f'/api/workstation/private-assets/{asset_id}/media?actor_number=A', headers=headers).content, self.data)
        self.assertEqual(self.client.get('/api/workstation/private-assets?actor_number=B', headers=headers).json()['total'], 0)

    def test_hash_mismatch_never_becomes_ready(self):
        asset_id = self.start()
        self.call('PUT', f'/api/private-assets/{asset_id}/upload?offset=0', content=b'x' * len(self.data))
        self.assertEqual(self.call('POST', f'/api/private-assets/{asset_id}/complete').status_code, 422)
        self.assertEqual(self.call('GET', '/api/private-assets').json()['total'], 0)
        self.assertEqual(self.start(), asset_id)
        self.assertEqual(self.call('GET', f'/api/private-assets/{asset_id}/upload').json()['offset'], 0)
        self.assertEqual(len(list(Path(self.directory.name).glob('*.invalid'))), 1)
        self.call('PUT', f'/api/private-assets/{asset_id}/upload?offset=0', content=self.data)
        self.assertEqual(self.call('POST', f'/api/private-assets/{asset_id}/complete').status_code, 200)


    def test_gray_rollout_denies_nonpilot_and_revocation_immediately(self):
        asset_id = self.ready('A')
        with patch.dict(os.environ, {'PRIVATE_ASSET_ALLOWED_USERS': 'A'}):
            self.assertEqual(self.call('GET', '/api/private-assets', 'B').status_code, 403)
            self.assertEqual(self.call('POST', '/api/private-assets/uploads', 'B', json={
                'filename': 'other.mp4', 'size': len(self.data), 'sha256': hashlib.sha256(self.data).hexdigest()
            }).status_code, 403)
            self.assertEqual(self.client.get('/api/workstation/private-assets?actor_number=B',
                headers={'x-test-service': 'service'}).status_code, 403)
            self.assertEqual(self.call('GET', f'/api/private-assets/{asset_id}/media', 'B').status_code, 403)
        with patch.dict(os.environ, {'PRIVATE_ASSETS_ENABLED': 'false'}):
            self.assertEqual(self.call('GET', f'/api/private-assets/{asset_id}/media', 'A').status_code, 403)
        self.assertEqual(self.call('GET', f'/api/private-assets/{asset_id}/media', 'A').content, self.data)

    def test_rollout_does_not_accept_names_or_wildcard(self):
        from app.private_assets import private_access_enabled
        with patch.dict(os.environ, {'PRIVATE_ASSET_ALLOWED_USERS': '舒豪,*'}):
            self.assertFalse(private_access_enabled('FD-123'))
        with patch.dict(os.environ, {'PRIVATE_ASSET_ALLOWED_USERS': ''}):
            self.assertFalse(private_access_enabled('A'))

if __name__ == '__main__':
    unittest.main()
