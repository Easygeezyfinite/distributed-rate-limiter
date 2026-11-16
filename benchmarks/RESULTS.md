# Load test results

Run on 2026-11-12 against a real Redis 7.0.15 server (2 vCPU Linux box), using
`benchmarks/load_test_limiter.py` and the unmodified `RedisFixedWindowRateLimiter`.

Each worker process stands in for one API server instance with its own Redis
connection pool; every process runs 8 threads calling `allow_request()` back to back.
This measures the limiter's decision path (one MULTI/EXEC round trip to Redis),
not end-to-end HTTP through FastAPI/uvicorn.

## Throughput (4 processes x 8 threads, 10 s per run, 1,000 clients, limit never hit)

| run | decisions/s | p50 | p95 | p99 |
|-----|-------------|-----|-----|-----|
| 1 | 20,359 | 1.20 ms | 3.86 ms | 7.26 ms |
| 2 | 20,290 | 1.18 ms | 3.88 ms | 6.54 ms |
| 3 | 20,078 | 1.15 ms | 4.12 ms | 7.69 ms |

Median: ~20,300 decisions/s, p95 3.88 ms.

## Correctness (all 4 processes x 8 threads hammer ONE client key for 3 s)

Each run starts at least 2 s inside a 60 s window so it never straddles two windows.

| limit | concurrent requests | allowed | result |
|-------|---------------------|---------|--------|
| 100 | 58,627 | 100 | exact |
| 1,000 | 63,075 | 1,000 | exact |
| 5,000 | 60,345 | 5,000 | exact |
| 100 | 60,689 | 100 | exact |
| 1,000 | 59,897 | 1,000 | exact |
| 5,000 | 56,185 | 5,000 | exact |

Zero over-admissions across ~359K concurrent requests.

An earlier unaligned run let 2,000 through with limit 1,000: that run crossed a
window boundary, which is the documented fixed-window burst (up to 2x the limit
across a boundary), not a race. A sliding window or token bucket would remove it.

## Reproduce

```bash
docker start redis
source .venv/bin/activate
python3 benchmarks/load_test_limiter.py
```
