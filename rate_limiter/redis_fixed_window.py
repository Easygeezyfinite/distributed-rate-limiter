"""Redis-backed fixed-window rate limiter.

Same algorithm as fixed_window.py, but the counts live in Redis instead of a
Python dict. That way several API servers can share one set of counts: if a
client hits server A and then server B, both see the same number.

Each (client, window) pair gets its own key, for example:

    rate_limit:alice:28392011

where 28392011 is the window number (current time // window_seconds). When a
new window starts the key name changes, so the count "resets" without us
having to delete anything. Old keys clean themselves up through EXPIRE.

INCR and EXPIRE are sent together in one MULTI/EXEC transaction, so a crash can't
leave a key behind without an expiry.
"""

import time


class RedisFixedWindowRateLimiter:
    def __init__(self, redis_client, limit, window_seconds, key_prefix="rate_limit", clock=time.time):
        if limit <= 0:
            raise ValueError("limit must be a positive number of requests")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self.redis = redis_client
        self.limit = limit
        self.window_seconds = window_seconds
        self.key_prefix = key_prefix
        # Wall-clock time (time.time), not time.monotonic like the in-memory version.
        # Every server has to agree on which window "now" is in, and monotonic clocks
        # start from an arbitrary point that is different on each machine.
        self.clock = clock

    def _key_for(self, client_id):
        window_number = int(self.clock() // self.window_seconds)
        return f"{self.key_prefix}:{client_id}:{window_number}"

    def allow_request(self, client_id):
        """Return True if this request is allowed, False if the client is over the limit."""
        key = self._key_for(client_id)

        # INCR on its own is atomic: if two servers call it at the same moment, one
        # gets 1 and the other gets 2, so there's no read-check-write race.
        #
        # But INCR + EXPIRE used to be two separate commands. If the process died
        # between them, the key was left with no expiry and sat in Redis forever.
        # Sending both inside a MULTI/EXEC transaction (redis-py pipelines use one by
        # default) means Redis applies both or neither.
        #
        # EXPIRE ... NX only sets the expiry if the key doesn't have one yet, so it's
        # safe to send on every request: the first request in a window sets the
        # lifetime and later ones don't push it back. That replaces the old
        # "if count == 1" check, which needed the INCR result before deciding.
        # (NX needs Redis 7+; the redis:7 Docker image is fine.)
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.incr(key)
        pipeline.expire(key, self.window_seconds, nx=True)
        request_count, _expiry_was_set = pipeline.execute()

        # Unlike the in-memory version, rejected requests still bump the counter.
        # That's fine: the comparison is still "more than limit -> reject", and the
        # key disappears when the window ends anyway.
        return request_count <= self.limit

    def remaining_requests(self, client_id):
        """How many more requests the client can make in the current window."""
        stored_count = self.redis.get(self._key_for(client_id))
        used = int(stored_count) if stored_count is not None else 0
        # The counter can go above the limit because rejected requests are counted,
        # so clamp at zero instead of reporting a negative number.
        return max(self.limit - used, 0)

    def seconds_until_reset(self):
        """Seconds until the current window ends (what a client should wait after a 429).

        Windows line up with wall-clock time (window_number = now // window_seconds),
        so every client's window ends at the same moment and Redis isn't needed here.
        """
        now = self.clock()
        return self.window_seconds - (now % self.window_seconds)
