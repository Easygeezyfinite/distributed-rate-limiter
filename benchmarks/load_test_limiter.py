"""Load test for RedisFixedWindowRateLimiter against a real Redis 7 server.

Each worker PROCESS plays the role of one API server instance (its own Redis connection pool),
and each process runs several THREADS that fire allow_request() calls back to back.

Two experiments:
  1. throughput_run    - many clients, high limit, measure decisions/sec + per-call latency percentiles
  2. correctness_run   - ONE shared key hammered by every process at once with a small limit;
                         the number of allowed requests must equal the limit exactly (no over-admission)

Usage (from the repo root, Redis on localhost:6379):
    python3 benchmarks/load_test_limiter.py
    python3 benchmarks/load_test_limiter.py --correctness-only
"""
import multiprocessing as mp
import os
import statistics
import sys
import threading
import time
import uuid

# The repo root is one folder up from this file, so the script works from any directory.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def import_limiter():
    # Imported inside the worker so each forked process builds its own Redis connection pool.
    sys.path.insert(0, REPO_ROOT)
    import redis
    from rate_limiter.redis_fixed_window import RedisFixedWindowRateLimiter
    return redis, RedisFixedWindowRateLimiter


def worker_process(process_index, threads_per_process, duration_seconds, limit, key_prefix,
                   client_pool_size, result_queue):
    redis, RedisFixedWindowRateLimiter = import_limiter()
    redis_client = redis.Redis(host="localhost", port=6379, max_connections=threads_per_process + 2)
    limiter = RedisFixedWindowRateLimiter(redis_client, limit=limit, window_seconds=60, key_prefix=key_prefix)

    per_thread_latencies_ms = [[] for _ in range(threads_per_process)]
    per_thread_allowed = [0] * threads_per_process
    stop_at = time.perf_counter() + duration_seconds

    def hammer(thread_index):
        latencies = per_thread_latencies_ms[thread_index]
        allowed_count = 0
        request_number = 0
        while time.perf_counter() < stop_at:
            client_id = f"client-{(process_index * 1000 + thread_index + request_number) % client_pool_size}"
            started = time.perf_counter()
            if limiter.allow_request(client_id):
                allowed_count += 1
            latencies.append((time.perf_counter() - started) * 1000)
            request_number += 1
        per_thread_allowed[thread_index] = allowed_count

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(threads_per_process)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    all_latencies = [value for thread_values in per_thread_latencies_ms for value in thread_values]
    result_queue.put((all_latencies, sum(per_thread_allowed)))


def run_experiment(process_count, threads_per_process, duration_seconds, limit, client_pool_size):
    key_prefix = "bench-" + uuid.uuid4().hex[:8]  # fresh keys so runs don't share counts
    result_queue = mp.Queue()
    processes = [mp.Process(target=worker_process, args=(i, threads_per_process, duration_seconds, limit,
                                                         key_prefix, client_pool_size, result_queue))
                 for i in range(process_count)]
    wall_start = time.perf_counter()
    for process in processes:
        process.start()
    results = [result_queue.get() for _ in processes]
    for process in processes:
        process.join()
    wall_seconds = time.perf_counter() - wall_start
    latencies = sorted(value for process_latencies, _ in results for value in process_latencies)
    allowed_total = sum(allowed for _, allowed in results)
    return latencies, allowed_total, wall_seconds


def percentile(sorted_values, fraction):
    return sorted_values[min(len(sorted_values) - 1, int(fraction * len(sorted_values)))]


if __name__ == "__main__":
    mp.set_start_method("fork")

    if "--correctness-only" not in sys.argv:
        print("== Throughput: 4 server processes x 8 threads, 10 s, 1,000 clients, limit high enough to never reject ==")
        throughput_runs = []
        for run_number in range(3):
            latencies, _, _ = run_experiment(process_count=4, threads_per_process=8, duration_seconds=10,
                                             limit=10**9, client_pool_size=1000)
            decisions_per_second = len(latencies) / 10
            throughput_runs.append((decisions_per_second, percentile(latencies, 0.50),
                                    percentile(latencies, 0.95), percentile(latencies, 0.99)))
            print(f"run {run_number + 1}: {decisions_per_second:,.0f} decisions/s | p50 {throughput_runs[-1][1]:.2f} ms | "
                  f"p95 {throughput_runs[-1][2]:.2f} ms | p99 {throughput_runs[-1][3]:.2f} ms | n={len(latencies):,}")
        print("median over runs -> decisions/s %.0f, p95 %.2f ms" % (
            statistics.median(r[0] for r in throughput_runs), statistics.median(r[2] for r in throughput_runs)))

    print("\n== Correctness: every process hits ONE shared client key at once ==")
    for limit in (100, 1000, 5000, 100, 1000, 5000):
        while not (2 <= time.time() % 60 <= 50):  # start well inside a window so the 3 s run never straddles two
            time.sleep(0.2)
        latencies, allowed_total, _ = run_experiment(process_count=4, threads_per_process=8, duration_seconds=3,
                                                     limit=limit, client_pool_size=1)
        verdict = "EXACT" if allowed_total == limit else "MISMATCH"
        print(f"limit {limit:>5}: {len(latencies):,} concurrent requests -> {allowed_total} allowed "
              f"({len(latencies) - allowed_total:,} rejected) -> {verdict}")
