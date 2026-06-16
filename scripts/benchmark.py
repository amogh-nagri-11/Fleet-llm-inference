#!/usr/bin/env python3
"""
Concurrent load benchmark for the LLM Inference Engine gateway.

Fires N requests at a target endpoint with a bounded concurrency level and
reports throughput plus p50/p95/p99 latency.

Examples
--------
    # 50 requests, 10 in flight at once, against the direct /generate endpoint
    python scripts/benchmark.py -n 50 -c 10

    # benchmark the queued path instead
    python scripts/benchmark.py -n 50 -c 10 --endpoint /api/v1/queued/generate

    # custom host / model / prompt
    python scripts/benchmark.py --base-url http://localhost:8000 \
        --model llama3:latest --prompt "Write a haiku about databases."
"""
import argparse
import asyncio
import statistics
import time

import httpx


def percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in 0..100)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


async def one_request(client: httpx.AsyncClient, url: str, headers: dict,
                      payload: dict) -> tuple[float, bool, int]:
    start = time.perf_counter()
    try:
        resp = await client.post(url, json=payload, headers=headers)
        elapsed = time.perf_counter() - start
        ok = resp.status_code == 200
        return elapsed, ok, resp.status_code
    except Exception:
        return time.perf_counter() - start, False, 0


async def run(args) -> None:
    url = args.base_url.rstrip("/") + args.endpoint
    headers = {"x-api-key": args.api_key, "content-type": "application/json"}
    payload = {"prompt": args.prompt, "model": args.model}

    sem = asyncio.Semaphore(args.concurrency)
    latencies: list[float] = []
    statuses: list[int] = []

    print(f"Target     : {url}")
    print(f"Requests   : {args.n}  |  concurrency: {args.concurrency}")
    print(f"Model      : {args.model}")
    print(f"Prompt     : {args.prompt!r}")
    print("Running...\n")

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        async def worker():
            async with sem:
                return await one_request(client, url, headers, payload)

        wall_start = time.perf_counter()
        results = await asyncio.gather(*[worker() for _ in range(args.n)])
        wall = time.perf_counter() - wall_start

    for elapsed, ok, code in results:
        statuses.append(code)
        if ok:
            latencies.append(elapsed)

    ok_count = len(latencies)
    fail_count = args.n - ok_count
    latencies.sort()

    print("─" * 48)
    print(f"Completed      : {args.n} requests in {wall:.2f}s")
    print(f"Successful     : {ok_count}")
    print(f"Failed         : {fail_count}")
    print(f"Throughput     : {args.n / wall:.2f} req/s")
    if ok_count:
        print(f"Latency mean   : {statistics.mean(latencies) * 1000:8.1f} ms")
        print(f"Latency p50    : {percentile(latencies, 50) * 1000:8.1f} ms")
        print(f"Latency p95    : {percentile(latencies, 95) * 1000:8.1f} ms")
        print(f"Latency p99    : {percentile(latencies, 99) * 1000:8.1f} ms")
        print(f"Latency min    : {latencies[0] * 1000:8.1f} ms")
        print(f"Latency max    : {latencies[-1] * 1000:8.1f} ms")
    if fail_count:
        from collections import Counter
        print(f"Status codes   : {dict(Counter(statuses))}")
    print("─" * 48)


def main():
    p = argparse.ArgumentParser(description="LLM gateway benchmark")
    p.add_argument("-n", type=int, default=20, help="total number of requests")
    p.add_argument("-c", "--concurrency", type=int, default=5, help="concurrent requests in flight")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--endpoint", default="/api/v1/generate",
                   help="e.g. /api/v1/generate or /api/v1/queued/generate")
    p.add_argument("--api-key", default="dev-key")
    p.add_argument("--model", default="llama3:latest")
    p.add_argument("--prompt", default="In one sentence, what is a load balancer?")
    p.add_argument("--timeout", type=float, default=180.0)
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
