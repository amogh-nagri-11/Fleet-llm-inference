from memory.models import MemoryItem, MemoryKind
from memory.ranking import lexical_relevance, rank_memories, score_memories


def make_item(content, importance=0.5, last_used_at=0.0, access_count=0):
    item = MemoryItem(kind=MemoryKind.EPISODIC, content=content, importance=importance)
    item.last_used_at = last_used_at
    item.access_count = access_count
    return item


# ── lexical_relevance ────────────────────────────────────────

def test_relevance_zero_for_empty_query():
    assert lexical_relevance("", "auth tests failing") == 0.0


def test_relevance_zero_for_no_word_overlap():
    assert lexical_relevance("database migration", "unrelated content here") == 0.0


def test_relevance_higher_for_more_overlap():
    query = "fix auth token expiration bug"
    close = "auth token expiration bug in middleware"
    far = "auth is mentioned once here"
    assert lexical_relevance(query, close) > lexical_relevance(query, far)


def test_relevance_case_insensitive():
    assert lexical_relevance("Auth Bug", "auth bug") == lexical_relevance("auth bug", "auth bug")


# ── score_memories ───────────────────────────────────────────

def test_importance_weight_dominates_when_others_zeroed():
    low = make_item("x", importance=0.1)
    high = make_item("y", importance=0.9)
    weights = {"relevance": 0, "importance": 1.0, "recency": 0, "access_frequency": 0}

    scores = score_memories([low, high], weights=weights)
    assert scores[high.id] > scores[low.id]


def test_recency_weight_prefers_more_recently_used():
    old = make_item("x", last_used_at=1.0)
    recent = make_item("y", last_used_at=100.0)
    weights = {"relevance": 0, "importance": 0, "recency": 1.0, "access_frequency": 0}

    scores = score_memories([old, recent], weights=weights)
    assert scores[recent.id] > scores[old.id]


def test_access_frequency_weight_prefers_more_used():
    rare = make_item("x", access_count=1)
    frequent = make_item("y", access_count=50)
    weights = {"relevance": 0, "importance": 0, "recency": 0, "access_frequency": 1.0}

    scores = score_memories([rare, frequent], weights=weights)
    assert scores[frequent.id] > scores[rare.id]


def test_relevance_weight_prefers_matching_query():
    off_topic = make_item("database migration notes")
    on_topic = make_item("auth token expiration bug")
    weights = {"relevance": 1.0, "importance": 0, "recency": 0, "access_frequency": 0}

    scores = score_memories([off_topic, on_topic], query="auth token bug", weights=weights)
    assert scores[on_topic.id] > scores[off_topic.id]


def test_score_memories_handles_single_item_without_dividing_by_zero():
    item = make_item("solo item")
    scores = score_memories([item])
    assert scores[item.id] >= 0.0


# ── rank_memories ────────────────────────────────────────────

def test_rank_memories_sorts_descending():
    a = make_item("a")
    b = make_item("b")
    scores = {a.id: 0.2, b.id: 0.9}
    ranked = rank_memories([a, b], scores)
    assert ranked == [b, a]
