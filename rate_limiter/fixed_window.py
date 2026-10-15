"""In-memory fixed-window rate limiter.

Idea: chop time into fixed buckets (e.g. 0-60s, 60-120s, ...). Each client
gets `limit` requests per bucket. When a new bucket starts, the count resets.
"""

import time


class FixedWindowRateLimiter:
    def __init__(self, limit, window_seconds, clock=time.monotonic):
        if limit <= 0:
            raise ValueError("limit must be a positive number of requests")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self.limit = limit
        self.window_seconds = window_seconds
        # The clock is passed in so tests can control time instead of sleeping.
        self.clock = clock
        # client_id -> (start time of the client's current window, requests counted in it)
        self.counts_by_client = {}

    def _current_window_start(self):
        now = self.clock()
        # Round down to the start of the bucket we're in.
        # e.g. window=60, now=125 -> 125 // 60 = 2 -> window starts at 120
        return (now // self.window_seconds) * self.window_seconds

    def allow_request(self, client_id):
        """Return True if this request is allowed, False if the client is over the limit."""
        window_start = self._current_window_start()
        saved_window_start, request_count = self.counts_by_client.get(client_id, (window_start, 0))

        # A new window started since this client's last request, so their count resets.
        if saved_window_start != window_start:
            request_count = 0

        if request_count >= self.limit:
            # Rejected requests are not counted, so they don't push the client further over.
            self.counts_by_client[client_id] = (window_start, request_count)
            return False

        self.counts_by_client[client_id] = (window_start, request_count + 1)
        return True

    def remaining_requests(self, client_id):
        """How many more requests the client can make in the current window."""
        window_start = self._current_window_start()
        saved_window_start, request_count = self.counts_by_client.get(client_id, (window_start, 0))
        if saved_window_start != window_start:
            return self.limit
        return self.limit - request_count
