"""ASGI middleware that puts a rate limiter in front of every request.

Written as plain ASGI (a class with an async __call__) instead of using
Starlette's BaseHTTPMiddleware. FastAPI accepts it the same way through
app.add_middleware, and because it doesn't import FastAPI or Starlette it
can be unit tested with nothing installed.

Over the limit -> 429 Too Many Requests with a Retry-After header telling the
client how many seconds to wait before the window resets.
"""

import json
import logging
import math

logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    def __init__(self, app, limiter, api_key_header="x-api-key", exempt_paths=("/health",)):
        self.app = app
        self.limiter = limiter
        # ASGI gives header names in lowercase bytes.
        self.api_key_header = api_key_header.lower().encode()
        self.exempt_paths = set(exempt_paths)

    def _client_id(self, scope):
        """Who is this request from? The API key if there is one, otherwise the IP."""
        for name, value in scope.get("headers", []):
            if name == self.api_key_header and value:
                return "key:" + value.decode("latin-1")
        # scope["client"] can be None (e.g. some test clients, unix sockets).
        client = scope.get("client")
        if client:
            return "ip:" + client[0]
        return "ip:unknown"

    async def __call__(self, scope, receive, send):
        # Only limit HTTP requests. "lifespan" (startup/shutdown) and websockets pass straight through.
        if scope["type"] != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        client_id = self._client_id(scope)
        try:
            # Note: with the Redis limiter this is a blocking network call inside an
            # async function. Against a local Redis it takes well under a millisecond,
            # but redis.asyncio would be the next step if it ever showed up in latency.
            allowed = self.limiter.allow_request(client_id)
        except Exception:
            # If Redis is down, fail open: let the request through and log it. Taking
            # the whole API down because the limiter's storage is down seemed worse
            # than briefly not limiting.
            logger.exception("rate limiter failed, allowing request from %s", client_id)
            allowed = True

        if allowed:
            await self.app(scope, receive, send)
            return

        await self._send_429(send)

    async def _send_429(self, send):
        # Retry-After has to be a whole number of seconds. Round up so a client that
        # waits exactly that long is in the next window, and never send 0.
        retry_after = max(1, math.ceil(self.limiter.seconds_until_reset()))
        body = json.dumps({"detail": "Too many requests"}).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(retry_after).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
