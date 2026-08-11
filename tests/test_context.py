from context.manager import ContextManager
from context.models import ContextItem, ContextType, estimate_tokens
from context.store import ContextStore


# ── ContextItem / estimate_tokens ─────────────────────────────

def test_estimate_tokens_roughly_four_chars_per_token():
    assert estimate_tokens("a" * 40) == 10


def test_estimate_tokens_never_zero_for_nonempty_text():
    assert estimate_tokens("hi") == 1


def test_context_item_computes_token_count_on_creation():
    item = ContextItem(type=ContextType.FILE, content="x" * 100)
    assert item.token_count == 25


def test_context_item_touch_updates_last_accessed_at():
    item = ContextItem(type=ContextType.CONVERSATION, content="hi")
    original = item.last_accessed_at
    item.last_accessed_at = 0.0
    item.touch()
    assert item.last_accessed_at != 0.0
    assert item.last_accessed_at >= original


# ── ContextStore ───────────────────────────────────────────────

def test_store_add_and_get():
    store = ContextStore()
    item = ContextItem(type=ContextType.FILE, content="hello", workflow_id="wf-1")
    store.add(item)
    assert store.get(item.id) is item


def test_store_get_missing_returns_none():
    store = ContextStore()
    assert store.get("does-not-exist") is None


def test_store_list_for_workflow_scopes_correctly():
    store = ContextStore()
    a = ContextItem(type=ContextType.FILE, content="a", workflow_id="wf-1")
    b = ContextItem(type=ContextType.FILE, content="b", workflow_id="wf-2")
    store.add(a)
    store.add(b)

    assert store.list_for_workflow("wf-1") == [a]
    assert store.list_for_workflow("wf-2") == [b]
    assert store.list_for_workflow("wf-missing") == []


def test_store_list_for_workflow_filters_by_type():
    store = ContextStore()
    file_item = ContextItem(type=ContextType.FILE, content="a", workflow_id="wf-1")
    tool_item = ContextItem(type=ContextType.TOOL_RESULT, content="b", workflow_id="wf-1")
    store.add(file_item)
    store.add(tool_item)

    assert store.list_for_workflow("wf-1", type=ContextType.FILE) == [file_item]
    assert store.list_for_workflow("wf-1", type=ContextType.TOOL_RESULT) == [tool_item]


def test_store_delete():
    store = ContextStore()
    item = ContextItem(type=ContextType.FILE, content="a", workflow_id="wf-1")
    store.add(item)

    assert store.delete(item.id) is True
    assert store.get(item.id) is None
    assert store.list_for_workflow("wf-1") == []
    assert store.delete(item.id) is False  # already gone


def test_store_clear_workflow():
    store = ContextStore()
    store.add(ContextItem(type=ContextType.FILE, content="a", workflow_id="wf-1"))
    store.add(ContextItem(type=ContextType.FILE, content="b", workflow_id="wf-1"))
    store.add(ContextItem(type=ContextType.FILE, content="c", workflow_id="wf-2"))

    removed = store.clear_workflow("wf-1")
    assert removed == 2
    assert store.list_for_workflow("wf-1") == []
    assert len(store.list_for_workflow("wf-2")) == 1


def test_store_total_tokens():
    store = ContextStore()
    store.add(ContextItem(type=ContextType.FILE, content="x" * 40, workflow_id="wf-1"))
    store.add(ContextItem(type=ContextType.FILE, content="x" * 40, workflow_id="wf-1"))
    assert store.total_tokens("wf-1") == 20


# ── ContextManager ───────────────────────────────────────────

def test_manager_record_stores_item():
    manager = ContextManager()
    item = manager.record("hello world", ContextType.CONVERSATION, workflow_id="wf-1")
    assert item.workflow_id == "wf-1"
    assert manager.get_candidate_context("wf-1") == [item]


def test_manager_get_candidate_context_isolates_workflows():
    manager = ContextManager()
    manager.record("a", ContextType.CONVERSATION, workflow_id="wf-1")
    manager.record("b", ContextType.CONVERSATION, workflow_id="wf-2")

    wf1_items = manager.get_candidate_context("wf-1")
    assert len(wf1_items) == 1
    assert wf1_items[0].content == "a"


def test_manager_total_tokens():
    manager = ContextManager()
    manager.record("x" * 40, ContextType.FILE, workflow_id="wf-1")
    assert manager.total_tokens("wf-1") == 10
