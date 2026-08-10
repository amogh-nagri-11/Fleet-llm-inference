import asyncio
import uuid

import pytest
import pytest_asyncio

from config import settings
from router.load_balancer import load_balancer
from workers.worker_pool import WorkerPool


# ── _process_job (pure — no Redis involved) ────────────────────
# WorkerPool._process_job() doesn't touch self.redis/self.queue at all, so
# these test the dispatch/metadata-stripping regression guards directly,
# without needing a real or fake queue underneath.

class FakeWorker:
    stats = type("Stats", (), {"url": "http://fake"})()

    async def generate(self, **kwargs):
        return {"response": "ok"}


class StrictWorker:
    """Mirrors OllamaClient.generate's real signature (no **kwargs) so a
    leaked agent_id/workflow_id/context_tokens kwarg would raise TypeError
    instead of silently being absorbed."""
    stats = type("Stats", (), {"url": "http://fake"})()

    async def generate(self, model, prompt, stream=False):
        return {"response": "ok"}


@pytest.mark.asyncio
async def test_no_available_worker_returns_error_result(monkeypatch):
    """Regression test: pick_worker() used to be called outside the
    try/except, so a tripped circuit breaker silently dropped the job
    instead of producing a fast error result."""
    def raise_no_workers(**kwargs):
        raise RuntimeError("No available workers — all are unhealthy or circuit open")

    monkeypatch.setattr(load_balancer, "pick_worker", raise_no_workers)
    called = []
    monkeypatch.setattr(load_balancer, "record_failure", lambda url: called.append(url))

    pool = WorkerPool()
    request_id, result = await pool._process_job(
        {"request_id": "req-1", "model": "llama3", "prompt": "hi"}
    )

    assert request_id == "req-1"
    assert "error" in result
    assert called == []  # no worker was ever chosen, nothing should be blamed


@pytest.mark.asyncio
async def test_successful_job_returns_result_and_records_success(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: FakeWorker())
    called = []
    monkeypatch.setattr(load_balancer, "record_success", lambda url: called.append(url))

    pool = WorkerPool()
    request_id, result = await pool._process_job(
        {"request_id": "req-2", "model": "llama3", "prompt": "hi"}
    )

    assert request_id == "req-2"
    assert result["response"] == "ok"
    assert called == ["http://fake"]


@pytest.mark.asyncio
async def test_agent_metadata_is_stripped_before_dispatch_to_worker(monkeypatch):
    """Phase 2 regression guard: agent_id/workflow_id/parent_request_id
    ride along in the payload but must not be forwarded as inference
    kwargs. StrictWorker would raise TypeError if they leaked through."""
    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: StrictWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    pool = WorkerPool()
    request_id, result = await pool._process_job({
        "request_id": "req-3",
        "model": "llama3",
        "prompt": "hi",
        "agent_id": "coding-agent-42",
        "workflow_id": "workflow-123",
        "parent_request_id": "req-1",
    })

    assert result == {"response": "ok"}


@pytest.mark.asyncio
async def test_context_tokens_stripped_and_forwarded_to_pick_worker(monkeypatch):
    """Phase 9: context_tokens rides along in the payload but isn't an
    inference kwarg — must be popped before **job, and forwarded to
    pick_worker() instead."""
    captured = {}
    monkeypatch.setattr(
        load_balancer, "pick_worker", lambda **kwargs: captured.update(kwargs) or StrictWorker()
    )
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    pool = WorkerPool()
    _, result = await pool._process_job({
        "request_id": "req-4",
        "model": "llama3",
        "prompt": "hi",
        "context_tokens": 12345,
    })

    assert captured["context_tokens"] == 12345
    assert result == {"response": "ok"}  # StrictWorker would've raised if it had leaked into **job


# ── Full loop, against real Redis Streams (Phase 10) ───────────

@pytest_asyncio.fixture
async def pool():
    p = WorkerPool()
    suffix = uuid.uuid4().hex[:8]
    p.queue_key = f"llm:stream:test:{suffix}"
    p.dead_letter_key = f"llm:stream:test-dlq:{suffix}"
    await p.connect()
    yield p
    await p.redis.delete(p.queue_key, p.dead_letter_key)
    await p.redis.aclose()


async def run_briefly(coro_factory, seconds=0.3):
    task = asyncio.create_task(coro_factory())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_process_loop_stores_result_and_acks(monkeypatch, pool):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    await pool.enqueue("req-loop-1", {"model": "llama3", "prompt": "hi"})
    await run_briefly(pool._process_loop)

    result = await pool.get_result("req-loop-1", timeout=1)
    assert result["response"] == "ok"

    pending = await pool.redis.xpending(pool.queue_key, settings.QUEUE_CONSUMER_GROUP)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_recovery_loop_reprocesses_crashed_job(monkeypatch, pool):
    """Simulates a worker crash: a job is read (entering the PEL) but
    never acked, then the recovery loop should reclaim and complete it."""
    monkeypatch.setattr(settings, "QUEUE_PENDING_MIN_IDLE_MS", 0)
    monkeypatch.setattr(settings, "QUEUE_RECOVERY_INTERVAL_SECONDS", 0.05)
    pool.retry_policy.min_idle_ms = 0

    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    await pool.enqueue("req-crash-1", {"model": "llama3", "prompt": "hi"})
    await pool.queue.read(count=1, block_ms=100)  # "crashes" — never acked

    await run_briefly(pool._recovery_loop, seconds=0.3)

    result = await pool.get_result("req-crash-1", timeout=1)
    assert result is not None
    assert result["response"] == "ok"


@pytest.mark.asyncio
async def test_recovery_loop_dead_letters_after_max_retries(monkeypatch, pool):
    monkeypatch.setattr(settings, "QUEUE_PENDING_MIN_IDLE_MS", 0)
    pool.retry_policy.min_idle_ms = 0
    pool.retry_policy.max_retries = 1

    await pool.enqueue("req-dead-1", {"model": "llama3", "prompt": "hi"})
    await pool.queue.read(count=1, block_ms=100)  # delivery_count=1, "crashes"

    recovery = await pool.retry_policy.recover(pool.dead_letter)
    assert recovery.retryable == []
    assert len(recovery.dead_lettered) == 1

    dlq_entries = await pool.dead_letter.list_all()
    assert dlq_entries[0]["payload"]["request_id"] == "req-dead-1"
