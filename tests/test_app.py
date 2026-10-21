"""End-to-end test of the FastAPI app through its HTTP interface.

Needs fastapi and httpx (pip install -r requirements.txt). Skipped otherwise.
"""

import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

from rate_limiter import FixedWindowRateLimiter


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApp(unittest.TestCase):
    def setUp(self):
        from app import create_app

        self.client = TestClient(create_app(FixedWindowRateLimiter(limit=3, window_seconds=60)))

    def test_fourth_request_gets_429(self):
        statuses = [self.client.get("/hello", headers={"X-API-Key": "alice"}).status_code for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 200, 429])

    def test_429_has_retry_after_and_json_body(self):
        for _ in range(3):
            self.client.get("/hello", headers={"X-API-Key": "alice"})
        response = self.client.get("/hello", headers={"X-API-Key": "alice"})
        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)
        self.assertEqual(response.json(), {"detail": "Too many requests"})

    def test_health_is_never_limited(self):
        statuses = [self.client.get("/health").status_code for _ in range(10)]
        self.assertEqual(statuses, [200] * 10)


if __name__ == "__main__":
    unittest.main()
