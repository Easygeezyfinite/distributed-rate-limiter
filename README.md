# Distributed Rate Limiter

A rate limiter written in Python, built in stages: first a simple in-memory version,
then a Redis-backed version that several API servers can share, served through FastAPI
and packaged with Docker.

**Status: in progress.** Only the in-memory fixed-window limiter exists so far.
See the roadmap below for what's done and what isn't.

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

## Project layout

```
rate_limiter/
  fixed_window.py      in-memory fixed-window limiter
tests/
  test_fixed_window.py unit tests (uses a fake clock, no sleeping)
```

## Running the tests

Requires Python 3.10+. No dependencies yet.

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

- [x] Repository setup and README
- [x] In-memory fixed-window limiter with unit tests
- [ ] Redis-backed limiter so multiple server instances share counts
- [ ] Concurrency / atomicity (two servers incrementing the same counter at once)
- [ ] FastAPI middleware returning 429 with `Retry-After`
- [ ] More tests and edge cases
- [ ] Docker + docker-compose (API + Redis)
- [ ] Extension: sliding window or token bucket

## Current limitations

- Counts live in one process's memory, so two servers wouldn't share limits (the Redis stage fixes this).
- Not thread-safe yet.
- Old clients are never removed from memory.
