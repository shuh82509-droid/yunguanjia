import unittest

from app.main import _central_auth_exempt


class QianchuanCallbackAccessTest(unittest.TestCase):
    def test_only_callback_bypasses_central_session_gate(self):
        self.assertTrue(_central_auth_exempt('/api/qianchuan/oauth/callback'))
        self.assertFalse(_central_auth_exempt('/api/qianchuan/oauth/start'))
        self.assertFalse(_central_auth_exempt('/api/qianchuan/status'))
        self.assertFalse(_central_auth_exempt('/api/qianchuan/deliveries'))


if __name__ == '__main__':
    unittest.main()
