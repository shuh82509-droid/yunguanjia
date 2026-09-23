import os, unittest, importlib.util
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('public_urls',Path(__file__).parents[1]/'app/public_urls.py')
urls=importlib.util.module_from_spec(spec);spec.loader.exec_module(urls)

class PublicUrlsTests(unittest.TestCase):
    def test_new_deployment_keeps_both_oauth_results_and_callback_on_new_site(self):
        with patch.dict(os.environ,{'WIS_PUBLIC_URL':'https://hub.fandow.com/yxb/wis-marketing-hub/modules/cloud-manager','QIANCHUAN_REDIRECT_URI':''}):
            root='https://hub.fandow.com/yxb/wis-marketing-hub/modules/cloud-manager/'
            self.assertEqual(urls.qianchuan_callback_uri(),root+'api/qianchuan/oauth/callback')
            self.assertEqual(urls.oauth_result_url('qianchuan',True),root+'?qianchuan=authorized')
            self.assertEqual(urls.oauth_result_url('adq_user',False),root+'?adq_user=authorization_failed')
    def test_explicit_registered_callback_is_preserved(self):
        with patch.dict(os.environ,{'QIANCHUAN_REDIRECT_URI':'https://example.test/registered-callback'}):
            self.assertEqual(urls.qianchuan_callback_uri(),'https://example.test/registered-callback')
    def test_old_deployment_default_remains_compatible(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(urls.oauth_result_url('qianchuan',False),urls.LEGACY_PUBLIC_URL+'?qianchuan=authorization_failed')
    def test_invalid_public_base_fails_instead_of_redirecting_ambiguously(self):
        for value in ['//example.test/','https://name:secret@example.test/','https://example.test/?next=other','https://example.test/#fragment']:
            with self.subTest(value=value), patch.dict(os.environ,{'WIS_PUBLIC_URL':value}):
                with self.assertRaises(ValueError):urls.public_root()

if __name__=='__main__':unittest.main()
