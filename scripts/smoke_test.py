#!/usr/bin/env python3
"""
Smoke test for the LLM Inference Engine gateway.

Hits every /api/v1 endpoint against a running gateway and checks for
correctness (status codes, auth enforcement, response shape) rather than
performance. Complements scripts/benchmark.py, which measures load/latency.

Requires the gateway to already be running (see CLAUDE.md "Running locally").
Inference calls (/generate, /chat, /queued/generate) can be slow on CPU —
use --skip-inference to check wiring/auth/health only.

Examples
--------
    python scripts/smoke_test.py
    python scripts/smoke_test.py --skip-inference
    python scripts/smoke_test.py --base-url http://localhost:8000 --api-key dev-key
"""
import argparse
import asyncio
import sys

import httpx

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = PASS if ok else FAIL
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


async def check_health(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    ok = resp.status_code == 200 and "queue_depth" in resp.json()
    record("GET /api/v1/health", ok, f"status={resp.status_code}")


async def check_auth_required(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/generate", json={"prompt": "hi"})
    record("POST /api/v1/generate rejects missing api key", resp.status_code == 401,
           f"status={resp.status_code}")

    resp = await client.get("/api/v1/workers", headers={"x-api-key": "wrong-key"})
    record("GET /api/v1/workers rejects bad api key", resp.status_code == 401,
           f"status={resp.status_code}")


async def check_workers(client: httpx.AsyncClient, headers: dict) -> None:
    resp = await client.get("/api/v1/workers", headers=headers)
    ok = resp.status_code == 200 and isinstance(resp.json(), list)
    record("GET /api/v1/workers", ok, f"status={resp.status_code}")


async def check_queue_depth(client: httpx.AsyncClient, headers: dict) -> None:
    resp = await client.get("/api/v1/queue/depth", headers=headers)
    ok = resp.status_code == 200 and "queue_depth" in resp.json()
    record("GET /api/v1/queue/depth", ok, f"status={resp.status_code}")


async def check_metrics(client: httpx.AsyncClient) -> None:
    resp = await client.get("/metrics")
    ok = resp.status_code == 200 and b"# HELP" in resp.content
    record("GET /metrics", ok, f"status={resp.status_code}")


async def check_generate(client: httpx.AsyncClient, headers: dict, model: str, timeout: float) -> None:
    try:
        resp = await client.post("/api/v1/generate", headers=headers, timeout=timeout,
                                  json={"prompt": "Say OK.", "model": model})
        ok = resp.status_code == 200 and "response" in resp.json()
        record("POST /api/v1/generate", ok, f"status={resp.status_code}")
    except httpx.TimeoutException:
        record("POST /api/v1/generate", False, "timed out")


async def check_chat(client: httpx.AsyncClient, headers: dict, model: str, timeout: float) -> None:
    try:
        resp = await client.post("/api/v1/chat", headers=headers, timeout=timeout,
                                  json={"messages": [{"role": "user", "content": "Say OK."}], "model": model})
        ok = resp.status_code == 200
        record("POST /api/v1/chat", ok, f"status={resp.status_code}")
    except httpx.TimeoutException:
        record("POST /api/v1/chat", False, "timed out")


async def check_queued_generate(client: httpx.AsyncClient, headers: dict, model: str, timeout: float) -> None:
    try:
        resp = await client.post("/api/v1/queued/generate", headers=headers, timeout=timeout,
                                  json={"prompt": "Say OK.", "model": model})
        ok = resp.status_code == 200 and "request_id" in resp.json()
        record("POST /api/v1/queued/generate", ok, f"status={resp.status_code}")
    except httpx.TimeoutException:
        record("POST /api/v1/queued/generate", False, "timed out")


async def run(args) -> int:
    headers = {"x-api-key": args.api_key, "content-type": "application/json"}

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        print(f"Target: {args.base_url}\n")

        await check_health(client)
        await check_auth_required(client)
        await check_workers(client, headers)
        await check_queue_depth(client, headers)
        await check_metrics(client)

        if args.skip_inference:
            print("\n(skipping inference checks — --skip-inference)")
        else:
            await check_generate(client, headers, args.model, args.inference_timeout)
            await check_chat(client, headers, args.model, args.inference_timeout)
            await check_queued_generate(client, headers, args.model, args.inference_timeout)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


def main():
    p = argparse.ArgumentParser(description="LLM gateway smoke test")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--api-key", default="dev-key")
    p.add_argument("--model", default="llama3:latest")
    p.add_argument("--skip-inference", action="store_true",
                    help="skip /generate, /chat, /queued/generate (slow on CPU)")
    p.add_argument("--inference-timeout", type=float, default=180.0)
    args = p.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
