# Distributed Rate Limiter

A rate limiter written in Python, built in stages: first a simple in-memory version,
then a Redis-backed version that several API servers can share, served through FastAPI
and packaged with Docker.

**Status:** in-memory and Redis-backed fixed-window limiters, a FastAPI app that
returns 429 with `Retry-After`, and a docker-compose setup running two API servers
against one Redis. A sliding-window algorithm is the main thing left (see the roadmap).

## What a rate limiter does

It caps how many requests a client can make in a period of time, for example
100 requests per minute per API key. Requests over the cap are rejected (an API
would return HTTP 429 Too Many Requests).

## Algorithm: fixed window

Time is split into fixed buckets of `window_seconds`. Each client gets `limit`
requests per bucket, and the count resets when the next bucket starts.

```
limit = 3, window = 10s

time:     0 ........ 9.9 | 10 ....... 19.9 | 20 ...
window:   [    0-10     ] [    10-20      ] [ ...
alice:    req req req X    req ...
                      ^ 4th request in the same window is rejected
```

**Why start here:** it's the simplest algorithm (one counter per client), it's easy to
test, and the same idea maps directly onto Redis (`INCR` + `EXPIRE`) later.

**Known weakness:** a client can send `limit` requests at the end of one window and
`limit` more at the start of the next, so up to 2x the limit can get through in a short
burst. `tests/test_fixed_window.py::test_boundary_burst_is_allowed` documents this.
Sliding-window or token-bucket algorithms fix it and are planned as an extension.

## Redis-backed version

`RedisFixedWindowRateLimiter` uses the same algorithm, but keeps counts in Redis so
several API servers can share them. Each client gets one key per window:

```
rate_limit:alice:28392011     (28392011 = current time // window_seconds)
```

- `INCR` the key on every request. Redis runs commands one at a time, so two servers
  incrementing at once get 1 and 2, never both 1.
- In the same request, `EXPIRE key window_seconds NX` so old windows delete themselves.
  `NX` means "only if the key has no expiry yet", so later requests don't push it back.
- Both commands go in one `MULTI`/`EXEC` transaction, so Redis runs both or neither.
  Before this, a server crashing between `INCR` and `EXPIRE` could leave a counter
  that never expired.
- Allow the request if the new count is `<= limit`.
- Wall-clock time is used so all servers agree on which window they're in.

I used a transaction instead of a Lua script because the logic doesn't need to read
the count before deciding what to do: `EXPIRE ... NX` removed the old
"if count == 1" branch. A Lua script would be the tool if it did. `NX` needs Redis 7+.

## HTTP API

`app.py` is a small FastAPI app with the limiter in front of it as middleware
(`rate_limiter/middleware.py`).

- Clients are identified by the `X-API-Key` header, or by IP address if there isn't one.
- Over the limit: `429 Too Many Requests`, a JSON body, and `Retry-After` set to the
  whole seconds left in the window (rounded up, never 0).
- `/health` is not rate limited.
- If Redis can't be reached the middleware lets requests through and logs an error
  (fail open), so a Redis outage doesn't take the API down with it.

Settings are environment variables: `RATE_LIMIT` (default 10), `WINDOW_SECONDS`
(default 60) and `REDIS_URL`. Without `REDIS_URL` counts are kept in memory.

Run it locally:

```bash
uvicorn app:app --reload
```

Then in another terminal, send 11 requests (the 11th should be `429`):

```bash
for i in $(seq 11); do curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: alice" localhost:8000/hello; done
```

To use the Redis container instead of memory:

```bash
REDIS_URL=redis://localhost:6379/0 uvicorn app:app --reload
```

## Docker

`docker-compose.yml` starts Redis and two copies of the API (ports 8001 and 8002), with
a limit of 5 requests per minute:

```bash
docker compose up --build -d
```

Alternate requests between the two servers. Because they share Redis, the 6th request
is rejected even though neither server has seen more than 3:

```bash
for port in 8001 8002 8001 8002 8001 8002; do curl -s -o /dev/null -w "$port %{http_code}\n" -H "X-API-Key: alice" localhost:$port/hello; done
```

Stop everything with:

```bash
docker compose down
```

The compose Redis doesn't publish a port, so it doesn't clash with a Redis already
running on `localhost:6379`.

## Project layout

```
app.py                       FastAPI app
rate_limiter/
  fixed_window.py            in-memory fixed-window limiter
  redis_fixed_window.py      Redis-backed fixed-window limiter
  middleware.py              ASGI middleware: 429 + Retry-After
tests/
  test_fixed_window.py       unit tests (uses a fake clock, no sleeping)
  test_redis_fixed_window.py tests against a fake Redis, plus real-Redis tests
  test_middleware.py         middleware tests (plain asyncio, no FastAPI needed)
  test_app.py                HTTP tests through FastAPI's TestClient
Dockerfile
docker-compose.yml
requirements.txt
```

## Running the tests

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

The real-Redis tests need a server on `localhost:6379` (see the setup below), and
`test_app.py` needs the packages from `requirements.txt`. Without them those tests are
skipped and everything else still runs. `REDIS_HOST` and `REDIS_PORT`
override the address.

## Setup for the Redis and Docker stages (macOS)

The in-memory limiter needs nothing extra. The Redis-backed version needs a Redis
server on `localhost:6379`. The simplest way is to run Redis in Docker, which the
project uses later anyway:

```bash
brew install --cask docker-desktop
open -a Docker
```

Accept the terms on first launch and wait until Docker says it's running. Then start Redis
and check it responds (it should print `PONG`):

```bash
docker run -d --name redis -p 6379:6379 --restart unless-stopped redis:7
docker exec redis redis-cli ping
```

I went with Docker instead of `brew install redis` because Homebrew had no prebuilt
Redis for my macOS setup and started compiling LLVM and Rust from source.

## Roadmap

- [x] Repository setup and README
- [x] In-memory fixed-window limiter with unit tests
- [x] Redis-backed limiter so multiple server instances share counts
- [x] Thread safety for the in-memory limiter (lock around read-check-write)
- [x] Atomic INCR + EXPIRE in Redis (MULTI/EXEC transaction with EXPIRE NX)
- [x] FastAPI middleware returning 429 with `Retry-After`
- [x] Edge-case tests (Retry-After rounding, missing/empty API key, Redis down)
- [x] Docker + docker-compose (two API servers + Redis)
- [ ] Extension: sliding window or token bucket

## Current limitations

- The in-memory limiter keeps counts in one process, so use the Redis version when running more than one server.
- Fixed windows allow a burst of up to 2x the limit across a window boundary.
- The middleware calls the synchronous Redis client from async code. Against a local
  Redis that's well under a millisecond, but `redis.asyncio` would avoid blocking the
  event loop.
- Behind a proxy or load balancer, every request has the proxy's IP, so clients without
  an API key would share one limit. Reading `X-Forwarded-For` from a trusted proxy would fix that.
- The in-memory limiter never removes old clients from memory.
