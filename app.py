"""Small FastAPI app to show the rate limiter working over HTTP.

Settings come from environment variables:
    RATE_LIMIT        requests allowed per window (default 10)
    WINDOW_SECONDS    window length in seconds (default 60)
    REDIS_URL         e.g. redis://localhost:6379/0. If unset, counts are kept
                      in memory, which only works with a single server process.
"""

import os

from fastapi import FastAPI

from rate_limiter import FixedWindowRateLimiter, RedisFixedWindowRateLimiter
from rate_limiter.middleware import RateLimitMiddleware


def build_limiter():
    limit = int(os.environ.get("RATE_LIMIT", "10"))
    window_seconds = int(os.environ.get("WINDOW_SECONDS", "60"))
    redis_url = os.environ.get("REDIS_URL")

    if redis_url:
        import redis

        return RedisFixedWindowRateLimiter(redis.Redis.from_url(redis_url), limit, window_seconds)
    return FixedWindowRateLimiter(limit, window_seconds)


def create_app(limiter=None):
    if limiter is None:
        limiter = build_limiter()
    app = FastAPI(title="Distributed Rate Limiter")
    app.add_middleware(RateLimitMiddleware, limiter=limiter)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    return app


app = create_app()
