import threading
import time
import unittest

from rate_limiter import FixedWindowRateLimiter


class FakeClock:
    """A clock we can move forward by hand, so tests don't need time.sleep()."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestFixedWindowRateLimiter(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = FixedWindowRateLimiter(limit=3, window_seconds=10, clock=self.clock)

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

        self.clock.advance(10)  # t=10 is the start of the next window
        self.assertTrue(self.limiter.allow_request("alice"))

    def test_clients_are_limited_separately(self):
        for _ in range(3):
            self.limiter.allow_request("alice")
        self.assertFalse(self.limiter.allow_request("alice"))
        self.assertTrue(self.limiter.allow_request("bob"))

    def test_remaining_requests(self):
        self.assertEqual(self.limiter.remaining_requests("alice"), 3)
        self.limiter.allow_request("alice")
        self.assertEqual(self.limiter.remaining_requests("alice"), 2)
        self.clock.advance(10)
        self.assertEqual(self.limiter.remaining_requests("alice"), 3)

    def test_rejected_requests_do_not_use_up_more_quota(self):
        for _ in range(5):
            self.limiter.allow_request("alice")
        self.assertEqual(self.limiter.remaining_requests("alice"), 0)

    def test_boundary_burst_is_allowed(self):
        # Known weakness of fixed windows: a client can send `limit` requests at
        # the very end of one window and `limit` more at the start of the next,
        # so 2x the limit gets through in about one second.
        self.clock.advance(9.5)
        end_of_first_window = [self.limiter.allow_request("alice") for _ in range(3)]
        self.clock.advance(0.5)  # t=10.0, new window
        start_of_second_window = [self.limiter.allow_request("alice") for _ in range(3)]
        self.assertEqual(end_of_first_window + start_of_second_window, [True] * 6)

    def test_seconds_until_reset(self):
        self.clock.advance(23)
        self.assertEqual(self.limiter.seconds_until_reset(), 7)

    def test_rejects_invalid_settings(self):
        with self.assertRaises(ValueError):
            FixedWindowRateLimiter(limit=0, window_seconds=10)
        with self.assertRaises(ValueError):
            FixedWindowRateLimiter(limit=3, window_seconds=0)


class SlowReadDict(dict):
    """A dict whose get() pauses for a moment.

    Without this, Python's GIL makes the race in allow_request so unlikely that the
    test passes even with no lock. The pause forces other threads to run between
    "read the count" and "write the count", which is exactly where the bug would be.
    """

    def get(self, key, default=None):
        value = super().get(key, default)
        time.sleep(0.0005)
        return value


class TestFixedWindowWithThreads(unittest.TestCase):
    def test_exact_limit_is_allowed_when_many_threads_share_a_client(self):
        # Freeze time so every request lands in the same window.
        limiter = FixedWindowRateLimiter(limit=100, window_seconds=60, clock=FakeClock(0))
        limiter.counts_by_client = SlowReadDict()
        allowed_results = []
        results_lock = threading.Lock()

        def send_requests():
            for _ in range(20):
                was_allowed = limiter.allow_request("alice")
                with results_lock:
                    allowed_results.append(was_allowed)

        # 10 threads x 20 requests = 200 attempts, but only 100 should get through.
        threads = [threading.Thread(target=send_requests) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(allowed_results), 200)
        self.assertEqual(allowed_results.count(True), 100)
        self.assertEqual(limiter.remaining_requests("alice"), 0)


if __name__ == "__main__":
    unittest.main()
