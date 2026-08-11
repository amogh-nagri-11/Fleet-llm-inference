from dataclasses import dataclass, field
from typing import Optional

from context.models import ContextItem

# REDESIGN.md §10 hybrid score weights. The formula there also has a
# workflow_weight * workflow_match term, but every candidate already comes
# pre-scoped to a single workflow (ContextStore.list_for_workflow), so that
# term is a constant 1.0 until Phase 7 introduces cross-workflow candidates
# (e.g. memory retrieval) — omitted rather than carried as dead weight.
DEFAULT_WEIGHTS = {
    "relevance": 0.4,
    "recency": 0.3,
    "importance": 0.3,
}

POLICIES = ("full", "recent", "relevance", "budget_aware", "hybrid")


@dataclass
class SelectionResult:
    selected: list[ContextItem]
    discarded: list[ContextItem]
    policy: str
    budget_tokens: int
    candidate_tokens: int = field(init=False)
    selected_tokens: int = field(init=False)

    def __post_init__(self):
        self.candidate_tokens = sum(i.token_count for i in self.selected) + sum(
            i.token_count for i in self.discarded
        )
        self.selected_tokens = sum(i.token_count for i in self.selected)

    @property
    def tokens_saved(self) -> int:
        return self.candidate_tokens - self.selected_tokens


def _recency_scores(items: list[ContextItem]) -> dict[str, float]:
    """Min-max normalize created_at within the candidate set — 1.0 is the
    most recent item, 0.0 the oldest. There's no fixed time horizon to
    normalize against, so this is relative to whatever's being selected
    from right now."""
    if not items:
        return {}
    times = [i.created_at for i in items]
    lo, hi = min(times), max(times)
    if hi == lo:
        return {i.id: 1.0 for i in items}
    return {i.id: (i.created_at - lo) / (hi - lo) for i in items}


def _hybrid_score(item: ContextItem, recency: float, weights: dict) -> float:
    return (
        weights["relevance"] * item.relevance
        + weights["recency"] * recency
        + weights["importance"] * item.importance
    )


def _greedy_pack(
    items: list[ContextItem], budget_tokens: int, key, reverse: bool = True
) -> tuple[list[ContextItem], list[ContextItem]]:
    """First-fit-decreasing-by-`key` greedy knapsack (REDESIGN.md §12 —
    'initially use a greedy strategy', not a full solver). Sorts candidates
    by `key` and takes each one that still fits, skipping (not stopping on)
    items that don't — a smaller item later in the list can still fit after
    a big one is skipped. With a plain ordering key (chronological,
    relevance) this is just "take items in that order until the budget runs
    out"; with a density key (value/token_count) it approximates a 0/1
    knapsack."""
    ordered = sorted(items, key=key, reverse=reverse)
    selected, discarded = [], []
    remaining = budget_tokens
    for item in ordered:
        if item.token_count <= remaining:
            selected.append(item)
            remaining -= item.token_count
        else:
            discarded.append(item)
    return selected, discarded


def select_context(
    items: list[ContextItem],
    budget_tokens: int,
    policy: str = "hybrid",
    weights: Optional[dict] = None,
) -> SelectionResult:
    """Select a subset of `items` that fits within `budget_tokens`, per
    REDESIGN.md §9-§12. Deterministic, no LLM involved (§10) — the policies
    mirror §11 exactly:

    - full: baseline, chronological order until the budget runs out (what a
      naive non-Fleet system does — just append history until it doesn't fit).
    - recent: most-recent-first until budget runs out.
    - relevance: highest-relevance-first until budget runs out.
    - budget_aware: greedy pack by importance/token_count density.
    - hybrid (recommended, §11 Policy 5): greedy pack by the weighted
      relevance+recency+importance score / token_count density.
    """
    if policy not in POLICIES:
        raise ValueError(f"Unknown selection policy: {policy!r} (expected one of {POLICIES})")

    if policy == "full":
        # Chronological, oldest first — what a naive non-Fleet system does:
        # just append history in order until it doesn't fit anymore.
        selected, discarded = _greedy_pack(
            items, budget_tokens, key=lambda i: i.created_at, reverse=False
        )
        return SelectionResult(selected, discarded, policy, budget_tokens)

    if policy == "recent":
        selected, discarded = _greedy_pack(items, budget_tokens, key=lambda i: i.created_at)
        return SelectionResult(selected, discarded, policy, budget_tokens)

    if policy == "relevance":
        selected, discarded = _greedy_pack(items, budget_tokens, key=lambda i: i.relevance)
        return SelectionResult(selected, discarded, policy, budget_tokens)

    if policy == "budget_aware":
        selected, discarded = _greedy_pack(
            items, budget_tokens, key=lambda i: i.importance / i.token_count
        )
        return SelectionResult(selected, discarded, policy, budget_tokens)

    # hybrid
    w = weights or DEFAULT_WEIGHTS
    recency = _recency_scores(items)
    selected, discarded = _greedy_pack(
        items, budget_tokens, key=lambda i: _hybrid_score(i, recency[i.id], w) / i.token_count
    )
    return SelectionResult(selected, discarded, policy, budget_tokens)
