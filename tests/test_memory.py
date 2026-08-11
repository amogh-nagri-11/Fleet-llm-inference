import time
import uuid

import pytest
import pytest_asyncio

from config import settings
from context.models import ContextType
from memory.manager import MemoryManager
from memory.models import MemoryItem, MemoryKind
from memory.store import MemoryStore


def wf_id() -> str:
    """Unique per-test workflow id so tests can run against the real DB
    without colliding, and are trivially cleaned up (see `store` fixture)."""
    return f"test-{uuid.uuid4()}"


@pytest_asyncio.fixture
async def store():
    s = MemoryStore()
    await s.connect(
        host=settings.MEMORY_DB_HOST,
        port=settings.MEMORY_DB_PORT,
        database=settings.MEMORY_DB_NAME,
        user=settings.MEMORY_DB_USER,
        password=settings.MEMORY_DB_PASSWORD or None,
    )
    yield s
    async with s.pool.acquire() as conn:
        await conn.execute("DELETE FROM memories WHERE workflow_id LIKE 'test-%'")
    await s.close()


@pytest_asyncio.fixture
async def manager(store):
    return MemoryManager(store=store)


# ── MemoryStore ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_and_get(store):
    workflow_id = wf_id()
    item = await store.add(
        _make_item(MemoryKind.EPISODIC, "attempted fix, test still failed", workflow_id)
    )
    fetched = await store.get(item.id)
    assert fetched.content == "attempted fix, test still failed"
    assert fetched.kind == MemoryKind.EPISODIC
    assert fetched.workflow_id == workflow_id


@pytest.mark.asyncio
async def test_get_missing_returns_none(store):
    assert await store.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_get_touches_last_used_and_access_count(store):
    workflow_id = wf_id()
    item = await store.add(_make_item(MemoryKind.EPISODIC, "x", workflow_id))
    assert item.access_count == 0

    fetched_once = await store.get(item.id)
    assert fetched_once.access_count == 1

    fetched_twice = await store.get(item.id)
    assert fetched_twice.access_count == 2
    assert fetched_twice.last_used_at >= fetched_once.last_used_at


@pytest.mark.asyncio
async def test_list_for_workflow_scopes_correctly(store):
    wf1, wf2 = wf_id(), wf_id()
    await store.add(_make_item(MemoryKind.EPISODIC, "a", wf1))
    await store.add(_make_item(MemoryKind.EPISODIC, "b", wf2))

    wf1_items = await store.list_for_workflow(wf1)
    assert len(wf1_items) == 1
    assert wf1_items[0].content == "a"


@pytest.mark.asyncio
async def test_list_for_workflow_filters_by_kind(store):
    workflow_id = wf_id()
    await store.add(_make_item(MemoryKind.WORKING, "current task", workflow_id))
    await store.add(_make_item(MemoryKind.EPISODIC, "past event", workflow_id))

    working = await store.list_for_workflow(workflow_id, kind=MemoryKind.WORKING)
    episodic = await store.list_for_workflow(workflow_id, kind=MemoryKind.EPISODIC)
    assert [i.content for i in working] == ["current task"]
    assert [i.content for i in episodic] == ["past event"]


@pytest.mark.asyncio
async def test_delete(store):
    workflow_id = wf_id()
    item = await store.add(_make_item(MemoryKind.EPISODIC, "a", workflow_id))

    assert await store.delete(item.id) is True
    assert await store.get(item.id) is None
    assert await store.delete(item.id) is False  # already gone


@pytest.mark.asyncio
async def test_purge_expired_removes_only_expired_working_memory(store):
    workflow_id = wf_id()
    expired = _make_item(MemoryKind.WORKING, "stale", workflow_id)
    expired.expires_at = time.time() - 10  # already in the past
    still_valid = _make_item(MemoryKind.WORKING, "fresh", workflow_id)
    still_valid.expires_at = time.time() + 3600
    episodic_never_expires = _make_item(MemoryKind.EPISODIC, "permanent", workflow_id)

    await store.add(expired)
    await store.add(still_valid)
    await store.add(episodic_never_expires)

    removed = await store.purge_expired()
    assert removed >= 1

    remaining = await store.list_for_workflow(workflow_id)
    remaining_contents = {i.content for i in remaining}
    assert "stale" not in remaining_contents
    assert "fresh" in remaining_contents
    assert "permanent" in remaining_contents


# ── MemoryManager ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manager_record_working_sets_expiry_from_ttl(manager):
    workflow_id = wf_id()
    before = time.time()
    item = await manager.record_working("current objective: fix auth", workflow_id, ttl_seconds=60)
    assert item.kind == MemoryKind.WORKING
    assert item.expires_at is not None
    assert before + 60 <= item.expires_at <= time.time() + 60


@pytest.mark.asyncio
async def test_manager_record_working_without_ttl_never_expires(manager):
    workflow_id = wf_id()
    item = await manager.record_working("current objective", workflow_id)
    assert item.expires_at is None


@pytest.mark.asyncio
async def test_manager_record_episodic_never_expires(manager):
    workflow_id = wf_id()
    item = await manager.record_episodic("test failed because of X", workflow_id)
    assert item.kind == MemoryKind.EPISODIC
    assert item.expires_at is None


@pytest.mark.asyncio
async def test_manager_list_for_workflow_isolates_workflows(manager):
    wf1, wf2 = wf_id(), wf_id()
    await manager.record_episodic("a", wf1)
    await manager.record_episodic("b", wf2)

    wf1_items = await manager.list_for_workflow(wf1)
    assert len(wf1_items) == 1
    assert wf1_items[0].content == "a"


# ── MemoryManager.get_relevant_memories / get_relevant_context (Phase 7) ─

@pytest.mark.asyncio
async def test_get_relevant_memories_picks_query_matching_item(manager):
    workflow_id = wf_id()
    await manager.record_episodic("auth token expiration bug in middleware", workflow_id, importance=0.5)
    await manager.record_episodic("unrelated database migration notes " * 20, workflow_id, importance=0.5)

    selected = await manager.get_relevant_memories(
        workflow_id, budget_tokens=20, query="auth token bug"
    )
    contents = [i.content for i in selected]
    assert any("auth token" in c for c in contents)
    assert not any("migration" in c for c in contents)


@pytest.mark.asyncio
async def test_get_relevant_memories_respects_budget(manager):
    workflow_id = wf_id()
    for i in range(5):
        await manager.record_episodic("x" * 400, workflow_id)  # ~100 tokens each

    selected = await manager.get_relevant_memories(workflow_id, budget_tokens=250)
    total_tokens = sum(max(1, len(i.content) // 4) for i in selected)
    assert total_tokens <= 250


@pytest.mark.asyncio
async def test_get_relevant_memories_filters_by_kind(manager):
    workflow_id = wf_id()
    await manager.record_working("current objective", workflow_id)
    await manager.record_episodic("past event", workflow_id)

    working_only = await manager.get_relevant_memories(
        workflow_id, budget_tokens=1000, kind=MemoryKind.WORKING
    )
    assert [i.content for i in working_only] == ["current objective"]


@pytest.mark.asyncio
async def test_get_relevant_context_returns_memory_type_context_items(manager):
    workflow_id = wf_id()
    await manager.record_episodic("test failed because of X", workflow_id, importance=0.9)

    context_items = await manager.get_relevant_context(workflow_id, budget_tokens=1000)
    assert len(context_items) == 1
    assert context_items[0].type == ContextType.MEMORY
    assert context_items[0].content == "test failed because of X"
    assert context_items[0].workflow_id == workflow_id


@pytest.mark.asyncio
async def test_get_relevant_memories_empty_workflow_returns_empty(manager):
    assert await manager.get_relevant_memories(wf_id(), budget_tokens=1000) == []


def _make_item(kind, content, workflow_id):
    return MemoryItem(kind=kind, content=content, workflow_id=workflow_id)
