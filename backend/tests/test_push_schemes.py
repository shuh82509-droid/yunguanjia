import os
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
import importlib.util
import unittest
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.database import Base, engine, get_db

# Load the candidate beside this test when run inside the frozen runtime.
candidate = Path(__file__).with_name('push_schemes.py')
if candidate.exists():
    spec = importlib.util.spec_from_file_location('app.push_schemes', candidate)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
else:
    from app import push_schemes as module


class SchemesTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.identity = 'employee-a'
        self.app = FastAPI()
        def require():
            if not self.identity:
                raise HTTPException(401)
            return {'number': self.identity}
        module.install_push_schemes(self.app, get_db, require, lambda u: u['number'])
        self.client = TestClient(self.app)
        self.url = '/api/push-schemes/qianchuan'
        self.payload = {'name': '水润方案', 'targets': [{'advertiser_id': '123', 'plan_id': '456',
                         'plan_type': 'multiplication', 'plan_name': '水润计划'}]}

    def test_owner_isolation(self):
        made = self.client.post(self.url, json=self.payload)
        self.assertEqual(made.status_code, 200)
        row = made.json()
        self.identity = 'employee-b'
        self.assertEqual(self.client.get(self.url).json()['items'], [])
        self.assertEqual(self.client.put(self.url+'/'+row['id'], json={**self.payload,'revision':1}).status_code,404)
        self.assertEqual(self.client.delete(self.url+'/'+row['id']+'?revision=1').status_code,404)
        self.identity = 'employee-a'
        self.assertEqual(len(self.client.get(self.url).json()['items']),1)

    def test_conflict_preserves_newer_data(self):
        row = self.client.post(self.url, json=self.payload).json()
        url = self.url+'/'+row['id']
        result = self.client.put(url,json={**self.payload,'name':'新方案','revision':1})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['revision'],2)
        self.assertEqual(self.client.put(url,json={**self.payload,'revision':1}).status_code,409)
        self.assertEqual(self.client.delete(url+'?revision=1').status_code,409)
        self.assertEqual(self.client.get(self.url).json()['items'][0]['name'],'新方案')
        self.assertEqual(self.client.delete(url+'?revision=2').status_code,200)

    def test_invalid_empty_or_duplicate_targets(self):
        for payload in [{**self.payload,'name':'  '},{**self.payload,'targets':[]},
                        {**self.payload,'targets':self.payload['targets']*2},
                        {**self.payload,'targets':[{**self.payload['targets'][0],'plan_type':'unknown'}]}]:
            self.assertEqual(self.client.post(self.url,json=payload).status_code,422)
        self.assertEqual(self.client.get(self.url).json()['items'],[])

    def test_requires_login(self):
        self.identity = ''
        self.assertEqual(self.client.get(self.url).status_code,401)
        self.assertEqual(self.client.post(self.url,json=self.payload).status_code,401)


if __name__ == '__main__':
    unittest.main()
