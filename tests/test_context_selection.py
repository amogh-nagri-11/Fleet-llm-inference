import pytest

from context.manager import ContextManager
from context.models import ContextItem, ContextType
from context.selection import SelectionResult, select_context


def make_item(content_len, importance=0.5, relevance=0.5, created_at=0.0, workflow_id="wf-1"):
    item = ContextItem(
        type=ContextType.CONVERSATION,
        content="x" * content_len,
        importance=importance,
        relevance=relevance,
        workflow_id=workflow_id,
    )
    item.created_at = created_at
    return item


# ── Basic budget respect ────────────────────────────────────

@pytest.mark.parametrize("policy", ["full", "recent", "relevance", "budget_aware", "hybrid"])
def test_every_policy_respects_the_budget(policy):
    items = [make_item(400, importance=i / 10, relevance=i / 10, created_at=i) for i in range(10)]
    result = select_context(items, budget_tokens=250, policy=policy)
    assert result.selected_tokens <= 250


def test_unknown_policy_raises():
    with pytest.raises(ValueError):
        select_context([], budget_tokens=100, policy="made_up")


def test_empty_items_returns_empty_result():
    result = select_context([], budget_tokens=1000, policy="hybrid")
    assert result.selected == []
    assert result.discarded == []
    assert result.candidate_tokens == 0
    assert result.tokens_saved == 0


def test_budget_larger_than_everything_selects_all():
    items = [make_item(40) for _ in range(5)]
    result = select_context(items, budget_tokens=100_000, policy="hybrid")
    assert len(result.selected) == 5
    assert result.discarded == []


# ── Policy-specific behavior ─────────────────────────────────

def test_recent_prefers_newest_items():
    old = make_item(400, created_at=1.0)
    new = make_item(400, created_at=100.0)
    result = select_context([old, new], budget_tokens=100, policy="recent")
    assert result.selected == [new]
    assert result.discarded == [old]


def test_full_prefers_oldest_items():
    old = make_item(400, created_at=1.0)
    new = make_item(400, created_at=100.0)
    result = select_context([old, new], budget_tokens=100, policy="full")
    assert result.selected == [old]
    assert result.discarded == [new]


def test_relevance_prefers_higher_relevance():
    low = make_item(400, relevance=0.1)
    high = make_item(400, relevance=0.9)
    result = select_context([low, high], budget_tokens=100, policy="relevance")
    assert result.selected == [high]


def test_budget_aware_prefers_higher_value_density():
    # Same size, but one has far higher importance per token.
    low_value = make_item(400, importance=0.1)
    high_value = make_item(400, importance=0.9)
    result = select_context([low_value, high_value], budget_tokens=100, policy="budget_aware")
    assert result.selected == [high_value]


def test_budget_aware_can_pack_multiple_small_items_over_one_big_item():
    big_low_value = make_item(400, importance=0.2)          # 100 tok, low density
    small_high_value_a = make_item(40, importance=0.9)       # 10 tok, high density
    small_high_value_b = make_item(40, importance=0.9)       # 10 tok, high density

    result = select_context(
        [big_low_value, small_high_value_a, small_high_value_b],
        budget_tokens=25,
        policy="budget_aware",
    )
    assert small_high_value_a in result.selected
    assert small_high_value_b in result.selected
    assert big_low_value not in result.selected


def test_hybrid_falls_back_correctly_when_all_signals_tie():
    a = make_item(40, importance=0.5, relevance=0.5, created_at=1.0)
    b = make_item(40, importance=0.5, relevance=0.5, created_at=1.0)
    result = select_context([a, b], budget_tokens=1000, policy="hybrid")
    assert a in result.selected and b in result.selected
    assert len(result.selected) == 2


def test_selection_result_tokens_saved():
    items = [make_item(400) for _ in range(3)]  # 100 tokens each
    result = select_context(items, budget_tokens=150, policy="recent")
    assert result.candidate_tokens == 300
    assert result.selected_tokens <= 150
    assert result.tokens_saved == result.candidate_tokens - result.selected_tokens


# ── ContextManager.get_budgeted_context ───────────────────────

def test_manager_get_budgeted_context_uses_selection():
    manager = ContextManager()
    manager.record("x" * 4000, ContextType.FILE, workflow_id="wf-1", importance=0.9)
    manager.record("y" * 4000, ContextType.FILE, workflow_id="wf-1", importance=0.1)

    result = manager.get_budgeted_context("wf-1", budget_tokens=1000, policy="budget_aware")
    assert isinstance(result, SelectionResult)
    assert result.selected_tokens <= 1000
    assert result.selected[0].content.startswith("x")  # higher importance wins
