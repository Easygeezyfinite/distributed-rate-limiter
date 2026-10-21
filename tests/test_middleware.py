"""Tests for RateLimitMiddleware.

The middleware is plain ASGI, so these tests call it directly with a fake
request (a scope dict) and collect whatever it sends back. No FastAPI needed.
"""

import asyncio
import unittest

from rate_limiter import FixedWindowRateLimiter
from rate_limiter.middleware import RateLimitMiddleware


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def ok_app(scope, receive, send):
    """Stands in for the real API: always answers 200."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def make_scope(path="/hello", api_key=None, ip="10.0.0.1", scope_type="http"):
    headers = []
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode()))
    return {
        "type": scope_type,
        "path": path,
        "headers": headers,
        "client": (ip, 50000) if ip else None,
    }


def call(middleware, scope):
    """Run one request through the middleware. Returns (status, headers dict, body)."""
    sent_messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent_messages.append(message)

    asyncio.run(middleware(scope, receive, send))

    start = sent_messages[0]
    headers = {name.decode(): value.decode() for name, value in start.get("headers", [])}
    body = b"".join(m.get("body", b"") for m in sent_messages[1:])
    return start["status"], headers, body


class BrokenLimiter:
    """Simulates the limiter's storage (Redis) being unreachable."""

    def allow_request(self, client_id):
        raise ConnectionError("redis is down")

    def seconds_until_reset(self):
        return 1


class TestRateLimitMiddleware(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = FixedWindowRateLimiter(limit=2, window_seconds=60, clock=self.clock)
        self.middleware = RateLimitMiddleware(ok_app, limiter=self.limiter)

    def test_requests_under_limit_reach_the_app(self):
        for _ in range(2):
            status, _, body = call(self.middleware, make_scope())
            self.assertEqual(status, 200)
            self.assertEqual(body, b"ok")

    def test_over_limit_gets_429_with_retry_after(self):
        call(self.middleware, make_scope())
        call(self.middleware, make_scope())
        self.clock.advance(15)

        status, headers, body = call(self.middleware, make_scope())

        self.assertEqual(status, 429)
        # 15s into a 60s window -> 45s left
        self.assertEqual(headers["retry-after"], "45")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertIn(b"Too many requests", body)

    def test_retry_after_rounds_up_and_is_never_zero(self):
        call(self.middleware, make_scope())
        call(self.middleware, make_scope())
        # 0.3s left in the window. Rounding down would tell the client "0".
        self.clock.advance(59.7)
        _, headers, _ = call(self.middleware, make_scope())
        self.assertEqual(headers["retry-after"], "1")

    def test_waiting_retry_after_seconds_works(self):
        call(self.middleware, make_scope())
        call(self.middleware, make_scope())
        self.clock.advance(10)
        _, headers, _ = call(self.middleware, make_scope())
        self.clock.advance(int(headers["retry-after"]))
        status, _, _ = call(self.middleware, make_scope())
        self.assertEqual(status, 200)

    def test_api_keys_are_limited_separately(self):
        call(self.middleware, make_scope(api_key="alice"))
        call(self.middleware, make_scope(api_key="alice"))
        alice_status, _, _ = call(self.middleware, make_scope(api_key="alice"))
        bob_status, _, _ = call(self.middleware, make_scope(api_key="bob"))
        self.assertEqual(alice_status, 429)
        self.assertEqual(bob_status, 200)

    def test_same_api_key_from_different_ips_shares_a_limit(self):
        call(self.middleware, make_scope(api_key="alice", ip="1.1.1.1"))
        call(self.middleware, make_scope(api_key="alice", ip="2.2.2.2"))
        status, _, _ = call(self.middleware, make_scope(api_key="alice", ip="3.3.3.3"))
        self.assertEqual(status, 429)

    def test_without_api_key_falls_back_to_ip(self):
        call(self.middleware, make_scope(ip="1.1.1.1"))
        call(self.middleware, make_scope(ip="1.1.1.1"))
        blocked, _, _ = call(self.middleware, make_scope(ip="1.1.1.1"))
        other_ip, _, _ = call(self.middleware, make_scope(ip="2.2.2.2"))
        self.assertEqual(blocked, 429)
        self.assertEqual(other_ip, 200)

    def test_empty_api_key_is_treated_as_missing(self):
        # Otherwise everyone sending "X-API-Key:" (empty) would share one bucket.
        call(self.middleware, make_scope(api_key="", ip="1.1.1.1"))
        call(self.middleware, make_scope(api_key="", ip="1.1.1.1"))
        status, _, _ = call(self.middleware, make_scope(api_key="", ip="2.2.2.2"))
        self.assertEqual(status, 200)

    def test_missing_client_address_does_not_crash(self):
        status, _, _ = call(self.middleware, make_scope(ip=None))
        self.assertEqual(status, 200)

    def test_health_check_is_not_limited(self):
        for _ in range(5):
            status, _, _ = call(self.middleware, make_scope(path="/health"))
            self.assertEqual(status, 200)
        # and it didn't use up the client's quota either
        self.assertEqual(self.limiter.remaining_requests("ip:10.0.0.1"), 2)

    def test_non_http_scopes_pass_through(self):
        seen_types = []

        async def recording_app(scope, receive, send):
            seen_types.append(scope["type"])

        middleware = RateLimitMiddleware(recording_app, limiter=self.limiter)
        asyncio.run(middleware({"type": "lifespan"}, None, None))
        self.assertEqual(seen_types, ["lifespan"])

    def test_fails_open_when_limiter_storage_is_down(self):
        middleware = RateLimitMiddleware(ok_app, limiter=BrokenLimiter())
        with self.assertLogs("rate_limiter.middleware", level="ERROR"):
            status, _, _ = call(middleware, make_scope())
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
