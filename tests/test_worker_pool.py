import asyncio
import json

import pytest

from workers.worker_pool import WorkerPool
from router.load_balancer import load_balancer


class FakeRedis:
    """Minimal stand-in for the aioredis calls _process_loop makes."""

    def __init__(self, jobs):
        self._jobs = list(jobs)
        self.stored: dict[str, str] = {}

    async def blpop(self, key, timeout):
        if self._jobs:
            return (key, self._jobs.pop(0))
        await asyncio.sleep(timeout)
        return None

    async def setex(self, key, ttl, value):
        self.stored[key] = value


async def run_one_iteration(pool: WorkerPool):
    task = asyncio.create_task(pool._process_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_no_available_worker_writes_error_result_instead_of_hanging(monkeypatch):
    """Regression test: pick_worker() used to be called outside the
    try/except, so a tripped circuit breaker silently dropped the job and
    the client polled forever instead of getting a fast error result."""
    job = json.dumps({"request_id": "req-1", "model": "llama3", "prompt": "hi"})
    redis = FakeRedis(jobs=[job])

    def raise_no_workers():
        raise RuntimeError("No available workers — all are unhealthy or circuit open")

    monkeypatch.setattr(load_balancer, "pick_worker", raise_no_workers)

    called = []
    monkeypatch.setattr(load_balancer, "record_failure", lambda url: called.append(url))

    pool = WorkerPool()
    pool.redis = redis

    await run_one_iteration(pool)

    assert "llm:result:req-1" in redis.stored
    result = json.loads(redis.stored["llm:result:req-1"])
    assert "error" in result
    # No worker was ever chosen, so nothing should be blamed via the circuit breaker.
    assert called == []


class FakeWorker:
    stats = type("Stats", (), {"url": "http://fake"})()

    async def generate(self, **kwargs):
        return {"response": "ok"}


@pytest.mark.asyncio
async def test_successful_job_writes_result_and_records_success(monkeypatch):
    job = json.dumps({"request_id": "req-2", "model": "llama3", "prompt": "hi"})
    redis = FakeRedis(jobs=[job])

    monkeypatch.setattr(load_balancer, "pick_worker", lambda: FakeWorker())
    called = []
    monkeypatch.setattr(load_balancer, "record_success", lambda url: called.append(url))

    pool = WorkerPool()
    pool.redis = redis

    await run_one_iteration(pool)

    result = json.loads(redis.stored["llm:result:req-2"])
    assert result["response"] == "ok"
    assert called == ["http://fake"]
