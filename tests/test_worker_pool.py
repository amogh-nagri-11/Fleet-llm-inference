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


# ── Regression: rejected jobs must not pollute context ─────────

@pytest.mark.asyncio
async def test_rejected_job_does_not_record_context(monkeypatch):
    from context.manager import context_manager

    def raise_no_capacity(**kwargs):
        raise RuntimeError("No available workers can handle N tokens of context")

    monkeypatch.setattr(load_balancer, "pick_worker", raise_no_capacity)

    workflow_id = f"test-wf-{uuid.uuid4()}"
    _, result = await WorkerPool()._process_job({
        "request_id": "req-5",
        "model": "llama3",
        "prompt": "x" * 2000,
        "workflow_id": workflow_id,
        "context_tokens": 99999,
    })

    assert "error" in result
    assert context_manager.total_tokens(workflow_id) == 0
    assert context_manager.get_candidate_context(workflow_id) == []


@pytest.mark.asyncio
async def test_accepted_job_does_record_context(monkeypatch):
    from context.manager import context_manager
    from context.models import ContextType

    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    workflow_id = f"test-wf-{uuid.uuid4()}"
    await WorkerPool()._process_job({
        "request_id": "req-6",
        "model": "llama3",
        "prompt": "hi",
        "workflow_id": workflow_id,
    })

    items = context_manager.get_candidate_context(workflow_id)
    assert len(items) == 1
    assert items[0].type == ContextType.CONVERSATION
    assert items[0].content == "hi"


# ── Full loop, against real Redis Streams (Phase 10) ───────────

@pytest_asyncio.fixture
async def pool():
    p = WorkerPool()
    suffix = uuid.uuid4().hex[:8]
    p.queue_key = f"llm:stream:test:{suffix}"
    p.dead_letter_key = f"llm:stream:test-dlq:{suffix}"
    await p.connect()
    yield p
    # A test may have already called pool.stop(), which closes and nulls
    # out self.redis — guard against tearing down twice.
    if p.redis:
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
async def test_two_worker_pools_share_queue_without_duplicate_processing(monkeypatch, pool):
    """Regression test for a live finding while brutally testing Phase 13:
    CLAUDE.md documents 'scale gateway replicas to add consumers' as a
    supported pattern, but it had never actually been tested. Verified
    live with two real gateway processes sharing one Redis Stream (jobs
    split 2/2 across replicas, zero duplication); this locks the
    guarantee in as an automated test — two WorkerPool instances racing
    for the same stream/group must process each job exactly once, not
    zero or two times."""
    pool2 = WorkerPool()
    pool2.queue_key = pool.queue_key
    pool2.dead_letter_key = pool.dead_letter_key
    await pool2.connect()

    call_count = {"n": 0}

    class CountingWorker:
        stats = type("Stats", (), {"url": "http://fake"})()

        async def generate(self, **kwargs):
            call_count["n"] += 1
            return {"response": "ok"}

    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: CountingWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    n = 8
    for i in range(n):
        await pool.enqueue(f"multi-req-{i}", {"model": "llama3", "prompt": f"hi {i}"})

    task1 = asyncio.create_task(pool._process_loop())
    task2 = asyncio.create_task(pool2._process_loop())
    await asyncio.sleep(0.5)
    for t in (task1, task2):
        t.cancel()
    for t in (task1, task2):
        try:
            await t
        except asyncio.CancelledError:
            pass

    # The direct proof: each job was picked up by exactly one of the two
    # pools, never both. If consumer-group exclusivity were broken (e.g.
    # a different group per pool, or a plain list-based queue), this
    # would be 2*n instead.
    assert call_count["n"] == n

    for i in range(n):
        stored = await pool.redis.get(f"llm:result:multi-req-{i}")
        assert stored is not None

    pending = await pool.redis.xpending(pool.queue_key, settings.QUEUE_CONSUMER_GROUP)
    assert pending["pending"] == 0

    await pool2.redis.aclose()


@pytest.mark.asyncio
async def test_stop_cancels_tasks_and_closes_redis(pool):
    """Regression test: stop() used to only cancel tasks and never close
    self.redis, leaking the connection pool on every shutdown."""
    pool.start()
    await asyncio.sleep(0.05)

    await pool.stop()

    assert pool._task.cancelled() or pool._task.done()
    assert pool._recovery_task.cancelled() or pool._recovery_task.done()
    assert pool.redis is None

    # Clean up the test's stream/DLQ keys with a fresh connection, since
    # pool.redis is gone now — this is what the `pool` fixture normally
    # does, but it guards against a double-close for exactly this case.
    import redis.asyncio as aioredis
    cleanup = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True,
    )
    await cleanup.delete(pool.queue_key, pool.dead_letter_key)
    await cleanup.aclose()


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
