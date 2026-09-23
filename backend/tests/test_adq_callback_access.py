import unittest

from app.main import _central_auth_exempt


class AdqCallbackAccessTests(unittest.TestCase):
    def test_callback_exempt_but_management_routes_remain_protected(self):
        self.assertTrue(_central_auth_exempt('/api/adq/user-authorization/callback'))
        self.assertFalse(_central_auth_exempt('/api/adq/user-authorization/start'))
        self.assertFalse(_central_auth_exempt('/api/adq/status'))
        self.assertFalse(_central_auth_exempt('/api/adq/deliveries'))


if __name__ == '__main__':
    unittest.main()
