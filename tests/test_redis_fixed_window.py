"""Tests for the Redis-backed limiter.

Two groups:
- TestRedisLimiterWithFakeRedis uses a tiny in-memory stand-in for Redis, so the
  logic can be tested anywhere, even without the redis package installed.
- TestRedisLimiterWithRealRedis talks to a real server on localhost:6379 and is
  skipped when the redis package or the server isn't available.
"""

import os
import threading
import unittest
import uuid

from rate_limiter import RedisFixedWindowRateLimiter


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeRedis:
    """Just enough of the Redis API for the limiter: incr, expire, get, ttl, pipeline.

    Values are stored as bytes because that's what the real client returns.
    Expiry is tracked against the same fake clock the limiter uses.

    Set drop_connection_mid_request = True to simulate the connection dying halfway
    through a request (e.g. the API server crashing):
    - commands sent one at a time: INCR reaches Redis, then EXPIRE fails
    - a MULTI/EXEC pipeline: EXEC never arrives, so Redis throws away the queued
      commands and none of them run
    """

    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.expires_at = {}
        self.expiry_sets = 0
        self.drop_connection_mid_request = False

    def _drop_if_expired(self, key):
        if key in self.expires_at and self.clock() >= self.expires_at[key]:
            del self.values[key]
            del self.expires_at[key]

    def incr(self, key):
        self._drop_if_expired(key)
        new_value = int(self.values.get(key, b"0")) + 1
        self.values[key] = str(new_value).encode()
        return new_value

    def expire(self, key, seconds, nx=False):
        if self.drop_connection_mid_request:
            raise ConnectionError("connection lost before EXPIRE")
        self._drop_if_expired(key)
        # Real Redis: EXPIRE on a missing key does nothing and returns False.
        if key not in self.values:
            return False
        # NX = only set an expiry if the key doesn't already have one.
        if nx and key in self.expires_at:
            return False
        self.expires_at[key] = self.clock() + seconds
        self.expiry_sets += 1
        return True

    def get(self, key):
        self._drop_if_expired(key)
        return self.values.get(key)

    def ttl(self, key):
        # Real Redis returns -1 for "exists but no expiry", -2 for "missing".
        self._drop_if_expired(key)
        if key not in self.values:
            return -2
        if key not in self.expires_at:
            return -1
        return int(self.expires_at[key] - self.clock())

    def pipeline(self, transaction=True):
        return FakePipeline(self, transaction)


class FakePipeline:
    """Queues commands and runs them all at once in execute(), like MULTI/EXEC."""

    def __init__(self, fake_redis, transaction):
        self.fake_redis = fake_redis
        self.transaction = transaction
        self.queued_commands = []

    def incr(self, key):
        self.queued_commands.append(("incr", (key,), {}))

    def expire(self, key, seconds, nx=False):
        self.queued_commands.append(("expire", (key, seconds), {"nx": nx}))

    def execute(self):
        if not self.transaction:
            raise AssertionError("limiter should use a MULTI/EXEC transaction")
        if self.fake_redis.drop_connection_mid_request:
            # EXEC never reached Redis, so nothing queued was applied.
            raise ConnectionError("connection lost before EXEC")
        results = []
        for name, args, kwargs in self.queued_commands:
            results.append(getattr(self.fake_redis, name)(*args, **kwargs))
        self.queued_commands = []
        return results


class TestRedisLimiterWithFakeRedis(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.fake_redis = FakeRedis(self.clock)
        self.limiter = RedisFixedWindowRateLimiter(
            self.fake_redis, limit=3, window_seconds=10, clock=self.clock
        )

    def test_allows_requests_up_to_the_limit(self):
        results = [self.limiter.allow_request("alice") for _ in range(3)]
        self.assertEqual(results, [True, True, True])

    def test_blocks_requests_over_the_limit(self):
        for _ in range(3):
            self.limiter.allow_request("alice")
        self.assertFalse(self.limiter.allow_request("alice"))
        self.assertFalse(self.limiter.allow_request("alice"))

    def test_count_resets_when_new_window_starts(self):
        for _ in range(3):
            self.limiter.allow_request("alice")
        self.assertFalse(self.limiter.allow_request("alice"))
        self.clock.advance(10)
        self.assertTrue(self.limiter.allow_request("alice"))

    def test_clients_have_separate_counts(self):
        for _ in range(3):
            self.limiter.allow_request("alice")
        self.assertFalse(self.limiter.allow_request("alice"))
        self.assertTrue(self.limiter.allow_request("bob"))

    def test_expiry_is_set_once_and_not_pushed_back(self):
        self.limiter.allow_request("alice")
        self.clock.advance(4)
        for _ in range(4):
            self.limiter.allow_request("alice")
        # Later requests send EXPIRE ... NX too, but it must not restart the
        # 10 second lifetime, otherwise a busy client's key would live forever.
        key = self.limiter._key_for("alice")
        self.assertEqual(self.fake_redis.ttl(key), 6)
        self.assertEqual(self.fake_redis.expiry_sets, 1)

    def test_crash_mid_request_never_leaves_key_without_expiry(self):
        self.fake_redis.drop_connection_mid_request = True
        with self.assertRaises(ConnectionError):
            self.limiter.allow_request("alice")
        # ttl == -1 would mean the counter exists with no expiry and never goes
        # away. With INCR + EXPIRE in one transaction, either both happened or
        # neither did.
        key = self.limiter._key_for("alice")
        self.assertNotEqual(self.fake_redis.ttl(key), -1)

    def test_seconds_until_reset(self):
        self.clock.advance(23)
        # window is 10s, so 23s is 3s into the 20-30 window
        self.assertEqual(self.limiter.seconds_until_reset(), 7)
        self.clock.advance(7)
        self.assertEqual(self.limiter.seconds_until_reset(), 10)

    def test_old_window_key_expires(self):
        self.limiter.allow_request("alice")
        old_key = self.limiter._key_for("alice")
        self.clock.advance(10)
        self.assertIsNone(self.fake_redis.get(old_key))

    def test_two_servers_share_one_count(self):
        # Two limiter objects pointing at the same Redis = two API servers.
        # Without shared storage each would allow 3, so 6 total.
        server_a = self.limiter
        server_b = RedisFixedWindowRateLimiter(
            self.fake_redis, limit=3, window_seconds=10, clock=self.clock
        )
        results = [
            server_a.allow_request("alice"),
            server_b.allow_request("alice"),
            server_a.allow_request("alice"),
            server_b.allow_request("alice"),
        ]
        self.assertEqual(results, [True, True, True, False])

    def test_remaining_requests(self):
        self.assertEqual(self.limiter.remaining_requests("alice"), 3)
        self.limiter.allow_request("alice")
        self.assertEqual(self.limiter.remaining_requests("alice"), 2)

    def test_remaining_requests_never_negative(self):
        # Rejected requests still increment the counter, so it can pass the limit.
        for _ in range(6):
            self.limiter.allow_request("alice")
        self.assertEqual(self.limiter.remaining_requests("alice"), 0)

    def test_rejects_bad_settings(self):
        with self.assertRaises(ValueError):
            RedisFixedWindowRateLimiter(self.fake_redis, limit=0, window_seconds=10)
        with self.assertRaises(ValueError):
            RedisFixedWindowRateLimiter(self.fake_redis, limit=3, window_seconds=0)


def connect_to_test_redis():
    """Return a connected client, or None if redis-py or the server is missing."""
    try:
        import redis
    except ImportError:
        return None
    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        socket_connect_timeout=1,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError:
        return None
    return client


real_redis = connect_to_test_redis()


@unittest.skipIf(real_redis is None, "no Redis server on localhost:6379 (or redis package not installed)")
class TestRedisLimiterWithRealRedis(unittest.TestCase):
    def setUp(self):
        # Random prefix so test keys never collide with anything else in Redis.
        self.key_prefix = f"test_rate_limit_{uuid.uuid4().hex}"
        # A long window so the test can't accidentally straddle a window boundary.
        self.limiter = RedisFixedWindowRateLimiter(
            real_redis, limit=5, window_seconds=3600, key_prefix=self.key_prefix
        )

    def tearDown(self):
        for key in real_redis.scan_iter(f"{self.key_prefix}:*"):
            real_redis.delete(key)

    def test_limit_is_enforced(self):
        results = [self.limiter.allow_request("alice") for _ in range(7)]
        self.assertEqual(results, [True] * 5 + [False] * 2)

    def test_key_has_expiry(self):
        self.limiter.allow_request("alice")
        ttl = real_redis.ttl(self.limiter._key_for("alice"))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 3600)

    def test_later_requests_do_not_push_expiry_back(self):
        import time

        self.limiter.allow_request("alice")
        time.sleep(0.2)
        self.limiter.allow_request("alice")
        # pttl is in milliseconds. If the second request had reset the expiry
        # it would be back near 3,600,000.
        remaining_ms = real_redis.pttl(self.limiter._key_for("alice"))
        self.assertLess(remaining_ms, 3600 * 1000 - 100)

    def test_threads_with_separate_connections_share_the_limit(self):
        # Each thread gets its own connection, like separate API servers would.
        import redis

        limit = 100
        allowed_count = 0
        count_lock = threading.Lock()

        def send_requests():
            nonlocal allowed_count
            connection = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                                     port=int(os.environ.get("REDIS_PORT", "6379")))
            server = RedisFixedWindowRateLimiter(
                connection, limit=limit, window_seconds=3600, key_prefix=self.key_prefix
            )
            for _ in range(20):
                if server.allow_request("alice"):
                    with count_lock:
                        allowed_count += 1
            connection.close()

        threads = [threading.Thread(target=send_requests) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 200 requests total against a limit of 100: exactly 100 should get through.
        self.assertEqual(allowed_count, limit)


if __name__ == "__main__":
    unittest.main()
