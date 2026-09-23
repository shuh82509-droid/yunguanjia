import unittest

from fastapi.testclient import TestClient

from app.main import app, _central_auth_exempt


class HubIdentityAccessTests(unittest.TestCase):
    def test_identity_lookup_skips_module_gate_but_still_requires_login(self):
        self.assertTrue(_central_auth_exempt("/api/auth/me"))
        self.assertFalse(_central_auth_exempt("/api/video-requests"))
        response = TestClient(app).get("/api/auth/me")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
