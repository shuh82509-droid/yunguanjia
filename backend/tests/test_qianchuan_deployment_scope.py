import os, unittest
from unittest.mock import patch,MagicMock
from datetime import datetime,timedelta,timezone
from app import qianchuan_service as module

class DeploymentScopeTests(unittest.TestCase):
 def setUp(self):
  self.data={'qianchuan.access_token':'test-only-access','qianchuan.refresh_token':'test-only-refresh'}
  self.db=MagicMock();self.service=module.QianchuanService()
  self.addCleanup(patch.stopall)
  patch.dict(os.environ,{'QIANCHUAN_APP_ID':'1','QIANCHUAN_APP_SECRET':'test-only-secret','QIANCHUAN_AUTHORIZATION_SCOPE':'new-site'}).start()
  patch.object(module,'_meta',side_effect=lambda db,key:self.data.get(key,'')).start()
  patch.object(module,'_set_meta',side_effect=lambda db,key,value:self.data.__setitem__(key,str(value))).start()
 def test_copied_tokens_are_neither_used_nor_refreshed(self):
  self.assertEqual(self.service._access_token(self.db),'');self.assertEqual(self.service._refresh_token(self.db),'')
  with patch.object(self.service,'_request_json') as request:
   with self.assertRaisesRegex(module.QianchuanError,'独立完成'):self.service.refresh_access_token(self.db)
   request.assert_not_called()
  self.assertEqual(self.data['qianchuan.refresh_token'],'test-only-refresh')
 def test_old_oauth_state_cannot_authorize_new_deployment(self):
  self.data.update({'qianchuan.oauth_state':'old-state','qianchuan.oauth_state_expires':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()})
  with patch.object(self.service,'_request_json') as request:
   with self.assertRaises(module.QianchuanError):self.service.exchange_code(self.db,'test-code','old-state')
   request.assert_not_called()
 def test_new_token_writer_records_deployment_scope(self):
  self.service._store_token_locked(self.db,{'access_token':'test-new-access','refresh_token':'test-new-refresh','expires_in':3600})
  self.assertEqual(self.data['qianchuan.authorization_scope'],'new-site')
  self.assertEqual(self.service._access_token(self.db),'test-new-access')
 def test_legacy_deployment_keeps_its_existing_authorization(self):
  with patch.dict(os.environ,{'QIANCHUAN_AUTHORIZATION_SCOPE':''}):
   self.assertEqual(self.service._access_token(self.db),'test-only-access')

if __name__=='__main__':unittest.main()
