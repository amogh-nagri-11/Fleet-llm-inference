import uuid

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from config import settings
from job_queue.dead_letter import DeadLetterQueue
from job_queue.retry import RetryPolicy
from job_queue.streams import RedisStreamQueue


def unique_key(prefix: str) -> str:
    return f"{prefix}:test:{uuid.uuid4()}"


@pytest_asyncio.fixture
async def redis_client():
    client = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        decode_responses=True,
    )
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def queue(redis_client):
    q = RedisStreamQueue(stream_key=unique_key("stream"), group_name="test-group")
    await q.connect(redis_client)
    yield q
    await redis_client.delete(q.stream_key)


@pytest_asyncio.fixture
async def dead_letter(redis_client):
    dlq = DeadLetterQueue(key=unique_key("dlq"))
    await dlq.connect(redis_client)
    yield dlq
    await redis_client.delete(dlq.key)


# ── RedisStreamQueue ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_and_read_roundtrip(queue):
    await queue.enqueue({"request_id": "r1", "prompt": "hi"})
    entries = await queue.read(count=1, block_ms=100)
    assert len(entries) == 1
    entry_id, payload = entries[0]
    assert payload == {"request_id": "r1", "prompt": "hi"}


@pytest.mark.asyncio
async def test_ack_removes_from_pending(queue):
    await queue.enqueue({"request_id": "r1"})
    [(entry_id, _)] = await queue.read(count=1, block_ms=100)
    await queue.ack(entry_id)

    pending = await queue.redis.xpending(queue.stream_key, queue.group_name)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_read_returns_empty_when_nothing_available(queue):
    entries = await queue.read(count=1, block_ms=50)
    assert entries == []


@pytest.mark.asyncio
async def test_depth_reflects_stream_length(queue):
    assert await queue.depth() == 0
    await queue.enqueue({"a": 1})
    await queue.enqueue({"b": 2})
    assert await queue.depth() == 2


@pytest.mark.asyncio
async def test_connect_is_idempotent(queue, redis_client):
    # Second connect() (same stream/group) must not raise BUSYGROUP.
    q2 = RedisStreamQueue(stream_key=queue.stream_key, group_name=queue.group_name)
    await q2.connect(redis_client)


@pytest.mark.asyncio
async def test_competing_consumers_do_not_get_the_same_message(redis_client):
    stream_key = unique_key("stream")
    q1 = RedisStreamQueue(stream_key=stream_key, group_name="g", consumer_name="c1")
    q2 = RedisStreamQueue(stream_key=stream_key, group_name="g", consumer_name="c2")
    await q1.connect(redis_client)
    await q2.connect(redis_client)

    await q1.enqueue({"n": 1})
    await q1.enqueue({"n": 2})

    got1 = await q1.read(count=5, block_ms=100)
    got2 = await q2.read(count=5, block_ms=100)

    all_ids = [e[0] for e in got1] + [e[0] for e in got2]
    assert len(all_ids) == 2
    assert len(set(all_ids)) == 2  # no overlap

    await redis_client.delete(stream_key)


# ── DeadLetterQueue ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_dead_letter_add_and_list(dead_letter):
    await dead_letter.add("orig-1", {"prompt": "hi"}, reason="max_retries_exceeded")
    entries = await dead_letter.list_all()
    assert len(entries) == 1
    assert entries[0]["original_entry_id"] == "orig-1"
    assert entries[0]["reason"] == "max_retries_exceeded"
    assert entries[0]["payload"] == {"prompt": "hi"}


@pytest.mark.asyncio
async def test_dead_letter_depth(dead_letter):
    assert await dead_letter.depth() == 0
    await dead_letter.add("orig-1", {}, reason="x")
    assert await dead_letter.depth() == 1


# ── RetryPolicy ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recover_returns_stale_entry_as_retryable(queue, dead_letter):
    policy = RetryPolicy(queue, max_retries=3, min_idle_ms=0)

    await queue.enqueue({"request_id": "r1"})
    await queue.read(count=1, block_ms=100)  # "crash" — never acked

    result = await policy.recover(dead_letter)
    assert len(result.retryable) == 1
    assert result.retryable[0][1] == {"request_id": "r1"}
    assert result.dead_lettered == []


@pytest.mark.asyncio
async def test_recover_dead_letters_after_max_retries_exceeded(queue, dead_letter):
    policy = RetryPolicy(queue, max_retries=1, min_idle_ms=0)

    await queue.enqueue({"request_id": "r1"})
    await queue.read(count=1, block_ms=100)  # delivery_count=1, "crashes"

    result = await policy.recover(dead_letter)  # reclaim -> delivery_count=2 > max_retries=1
    assert result.retryable == []
    assert len(result.dead_lettered) == 1

    dlq_entries = await dead_letter.list_all()
    assert dlq_entries[0]["payload"] == {"request_id": "r1"}

    # Dead-lettered entry was ACKed — nothing left pending to reclaim.
    pending = await queue.redis.xpending(queue.stream_key, queue.group_name)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_recover_finds_nothing_when_queue_is_empty(queue, dead_letter):
    policy = RetryPolicy(queue, max_retries=3, min_idle_ms=0)
    result = await policy.recover(dead_letter)
    assert result.retryable == []
    assert result.dead_lettered == []


@pytest.mark.asyncio
async def test_recover_ignores_entries_within_min_idle_time(queue, dead_letter):
    """A job that's still actively being processed (idle time below the
    threshold) shouldn't be reclaimed out from under its consumer."""
    policy = RetryPolicy(queue, max_retries=3, min_idle_ms=60_000)  # 60s — nothing is that idle yet

    await queue.enqueue({"request_id": "r1"})
    await queue.read(count=1, block_ms=100)

    result = await policy.recover(dead_letter)
    assert result.retryable == []
    assert result.dead_lettered == []
