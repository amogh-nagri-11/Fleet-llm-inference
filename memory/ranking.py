import re
from typing import Optional

from memory.models import MemoryItem

# REDESIGN.md §21's memory_score formula doesn't commit to specific weights
# the way §10 does for context selection, so this defaults to equal
# weighting — independently tunable, not hidden inside one giant function.
DEFAULT_WEIGHTS = {
    "relevance": 0.25,
    "importance": 0.25,
    "recency": 0.25,
    "access_frequency": 0.25,
}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def lexical_relevance(query: str, content: str) -> float:
    """Deterministic word-overlap relevance (Jaccard similarity between
    query and content word sets). Not semantic search — embeddings/vector
    similarity are explicitly deferred (§0.2). Consistent with the
    project's 'deterministic and measurable first' rule (§10), used
    everywhere else relevance is scored (context selection, Phase 4)."""
    if not query:
        return 0.0
    query_words, content_words = _words(query), _words(content)
    if not query_words or not content_words:
        return 0.0
    return len(query_words & content_words) / len(query_words | content_words)


def _normalize(values: dict[str, float]) -> dict[str, float]:
    """Min-max normalize within the candidate set, same approach as
    context/selection.py's recency scoring — no fixed horizon to compare
    against, so it's relative to whatever's being ranked right now."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return {k: 1.0 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def score_memories(
    items: list[MemoryItem], query: str = "", weights: Optional[dict] = None
) -> dict[str, float]:
    """REDESIGN.md §21: memory_score = relevance + importance + recency +
    access_frequency, as a weighted sum. Recency is based on
    `last_used_at` (not `created_at`) — decay is about staleness of
    *usage*, and last_used_at is exactly what MemoryStore.get() tracks."""
    w = weights or DEFAULT_WEIGHTS
    recency = _normalize({i.id: i.last_used_at for i in items})
    access = _normalize({i.id: float(i.access_count) for i in items})
    return {
        item.id: (
            w["relevance"] * lexical_relevance(query, item.content)
            + w["importance"] * item.importance
            + w["recency"] * recency[item.id]
            + w["access_frequency"] * access[item.id]
        )
        for item in items
    }


def rank_memories(items: list[MemoryItem], scores: dict[str, float]) -> list[MemoryItem]:
    """§19 'rank' step, exposed standalone from budgeting/injection so it's
    independently testable and usable on its own (e.g. inspecting ranked
    memories without a budget constraint)."""
    return sorted(items, key=lambda i: scores[i.id], reverse=True)
