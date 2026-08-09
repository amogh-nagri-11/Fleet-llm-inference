from context.models import ContextType
from memory.models import MemoryItem, MemoryKind
from memory.retrieval import (
    retrieve_relevant_memories,
    select_within_budget,
    to_context_items,
)


def make_item(content, importance=0.5):
    return MemoryItem(kind=MemoryKind.EPISODIC, content=content, importance=importance)


# ── select_within_budget ───────────────────────────────────────

def test_select_within_budget_respects_budget():
    items = [make_item("x" * 400) for _ in range(5)]  # ~100 tokens each
    scores = {i.id: 0.5 for i in items}
    selected = select_within_budget(items, scores, budget_tokens=250)
    total_tokens = sum(max(1, len(i.content) // 4) for i in selected)
    assert total_tokens <= 250


def test_select_within_budget_prefers_higher_density():
    cheap_valuable = make_item("x" * 40)   # ~10 tokens
    expensive_low_value = make_item("y" * 400)  # ~100 tokens
    scores = {cheap_valuable.id: 0.9, expensive_low_value.id: 0.1}

    selected = select_within_budget(
        [cheap_valuable, expensive_low_value], scores, budget_tokens=10
    )
    assert selected == [cheap_valuable]


def test_select_within_budget_empty_input():
    assert select_within_budget([], {}, budget_tokens=1000) == []


# ── to_context_items ─────────────────────────────────────────

def test_to_context_items_produces_memory_type():
    item = MemoryItem(
        kind=MemoryKind.WORKING,
        content="current objective: fix auth",
        agent_id="agent-1",
        workflow_id="wf-1",
        importance=0.8,
    )
    context_items = to_context_items([item])
    assert len(context_items) == 1
    ctx = context_items[0]
    assert ctx.type == ContextType.MEMORY
    assert ctx.content == "current objective: fix auth"
    assert ctx.source == "memory:working"
    assert ctx.agent_id == "agent-1"
    assert ctx.workflow_id == "wf-1"
    assert ctx.importance == 0.8


def test_to_context_items_empty():
    assert to_context_items([]) == []


# ── retrieve_relevant_memories (full pipeline over a pool) ────

def test_retrieve_relevant_memories_ranks_and_budgets():
    on_topic = make_item("auth token expiration bug", importance=0.9)
    off_topic = make_item("unrelated database migration notes " * 20, importance=0.9)

    selected = retrieve_relevant_memories(
        [on_topic, off_topic],
        budget_tokens=20,
        query="auth token bug",
    )
    assert on_topic in selected
    assert off_topic not in selected


def test_retrieve_relevant_memories_empty_input():
    assert retrieve_relevant_memories([], budget_tokens=1000) == []
