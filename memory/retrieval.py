from typing import Optional

from context.models import ContextItem, ContextType, estimate_tokens
from memory.models import MemoryItem
from memory.ranking import score_memories


def select_within_budget(
    items: list[MemoryItem], scores: dict[str, float], budget_tokens: int
) -> list[MemoryItem]:
    """REDESIGN.md §19: retrieval must respect the budget — do not retrieve
    everything and then discover there's no room left. Greedy pack by
    score/token-cost density, mirroring context/selection.py's approach
    (§12's 'initially use a greedy strategy')."""
    ordered = sorted(
        items,
        key=lambda i: scores[i.id] / max(1, estimate_tokens(i.content)),
        reverse=True,
    )
    selected, remaining = [], budget_tokens
    for item in ordered:
        cost = estimate_tokens(item.content)
        if cost <= remaining:
            selected.append(item)
            remaining -= cost
    return selected


def to_context_items(items: list[MemoryItem]) -> list[ContextItem]:
    """§19 'inject' step — converts retrieved memories into ContextItems
    (type=MEMORY) ready for the context pipeline. Does not add them to a
    live ContextStore itself — that wiring is Phase 9, same as every other
    context-producing phase so far (context abstraction, budgeting,
    artifacts)."""
    return [
        ContextItem(
            type=ContextType.MEMORY,
            content=item.content,
            source=f"memory:{item.kind.value}",
            agent_id=item.agent_id,
            workflow_id=item.workflow_id,
            importance=item.importance,
        )
        for item in items
    ]


def retrieve_relevant_memories(
    items: list[MemoryItem],
    budget_tokens: int,
    query: str = "",
    weights: Optional[dict] = None,
) -> list[MemoryItem]:
    """Full §19 pipeline (rank + budget) over an already-fetched candidate
    pool. The 'retrieve' step itself is a DB call
    (MemoryManager.list_for_workflow) — not something a pure function
    needs to know about."""
    if not items:
        return []
    scores = score_memories(items, query=query, weights=weights)
    return select_within_budget(items, scores, budget_tokens)
