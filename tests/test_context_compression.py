import pytest

from context.compression import CompressionResult, compress_items, llm_summarizer
from context.manager import ContextManager
from context.models import ContextItem, ContextType
from router.load_balancer import load_balancer


def make_item(content, importance=0.5, created_at=0.0, workflow_id="wf-1", type=ContextType.CONVERSATION):
    item = ContextItem(type=type, content=content, importance=importance, workflow_id=workflow_id)
    item.created_at = created_at
    return item


async def fake_summarizer(text: str) -> str:
    return f"SUMMARY[{len(text)} chars]"


# ── compress_items (pure) ────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_items_produces_summary_type():
    items = [make_item("a" * 400), make_item("b" * 400)]
    result = await compress_items(items, fake_summarizer)
    assert isinstance(result, CompressionResult)
    assert result.summary_item.type == ContextType.SUMMARY
    assert result.summary_item.source == "compression"


@pytest.mark.asyncio
async def test_compress_items_inherits_workflow_and_agent_id():
    items = [make_item("a", workflow_id="wf-42")]
    items[0].agent_id = "agent-7"
    result = await compress_items(items, fake_summarizer)
    assert result.summary_item.workflow_id == "wf-42"
    assert result.summary_item.agent_id == "agent-7"


@pytest.mark.asyncio
async def test_compress_items_explicit_workflow_id_overrides():
    items = [make_item("a", workflow_id="wf-1")]
    result = await compress_items(items, fake_summarizer, workflow_id="wf-override")
    assert result.summary_item.workflow_id == "wf-override"


@pytest.mark.asyncio
async def test_compress_items_importance_is_max_of_originals():
    items = [make_item("a", importance=0.2), make_item("b", importance=0.9)]
    result = await compress_items(items, fake_summarizer)
    assert result.summary_item.importance == 0.9


@pytest.mark.asyncio
async def test_compress_items_raises_on_empty_list():
    with pytest.raises(ValueError):
        await compress_items([], fake_summarizer)


@pytest.mark.asyncio
async def test_compression_result_tracks_tokens_saved():
    items = [make_item("x" * 4000), make_item("y" * 4000)]  # 1000 + 1000 tokens
    result = await compress_items(items, fake_summarizer)  # summary is tiny
    assert result.tokens_before == 2000
    assert result.tokens_after < 50
    assert result.tokens_saved == result.tokens_before - result.tokens_after
    assert result.tokens_saved > 1900


# ── llm_summarizer (real backend, mocked worker) ──────────────

class FakeWorker:
    def __init__(self, url="http://fake"):
        self.stats = type("Stats", (), {"url": url})()
        self.received_prompt = None
        self.received_model = None

    async def generate(self, model, prompt, stream=False):
        self.received_prompt = prompt
        self.received_model = model
        return {"response": "a concise summary"}


@pytest.mark.asyncio
async def test_llm_summarizer_calls_worker_and_returns_response(monkeypatch):
    worker = FakeWorker()
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: worker)
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    summary = await llm_summarizer("some long context text")
    assert summary == "a concise summary"
    assert "some long context text" in worker.received_prompt


@pytest.mark.asyncio
async def test_llm_summarizer_uses_explicit_model_when_given(monkeypatch):
    worker = FakeWorker()
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: worker)
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    await llm_summarizer("text", model="custom-model")
    assert worker.received_model == "custom-model"


@pytest.mark.asyncio
async def test_llm_summarizer_no_worker_available_does_not_blame_a_worker(monkeypatch):
    """Regression guard, same pattern as gateway/routes.py and
    worker_pool.py (Phase 1/2): pick_worker() failing shouldn't trigger
    record_failure() for a worker that was never actually picked."""
    def raise_no_workers():
        raise RuntimeError("No available workers — all are unhealthy or circuit open")

    monkeypatch.setattr(load_balancer, "pick_worker", raise_no_workers)
    called = []
    monkeypatch.setattr(load_balancer, "record_failure", lambda url: called.append(url))

    with pytest.raises(RuntimeError):
        await llm_summarizer("text")
    assert called == []


@pytest.mark.asyncio
async def test_llm_summarizer_worker_failure_records_failure_and_reraises(monkeypatch):
    class FailingWorker(FakeWorker):
        async def generate(self, model, prompt, stream=False):
            raise RuntimeError("Worker http://fake failed: boom")

    worker = FailingWorker()
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: worker)
    called = []
    monkeypatch.setattr(load_balancer, "record_failure", lambda url: called.append(url))

    with pytest.raises(RuntimeError):
        await llm_summarizer("text")
    assert called == ["http://fake"]


# ── ContextManager.compress_old_context ───────────────────────

@pytest.mark.asyncio
async def test_manager_compress_old_context_replaces_items_with_summary():
    manager = ContextManager()
    manager.record("old message 1", ContextType.CONVERSATION, workflow_id="wf-1")
    manager.record("old message 2", ContextType.CONVERSATION, workflow_id="wf-1")

    result = await manager.compress_old_context("wf-1", fake_summarizer)

    assert result is not None
    assert len(result.original_items) == 2

    remaining = manager.get_candidate_context("wf-1")
    assert len(remaining) == 1
    assert remaining[0].type == ContextType.SUMMARY


@pytest.mark.asyncio
async def test_manager_compress_old_context_respects_max_items():
    manager = ContextManager()
    for i in range(5):
        item = manager.record(f"message {i}", ContextType.CONVERSATION, workflow_id="wf-1")
        item.created_at = float(i)

    result = await manager.compress_old_context("wf-1", fake_summarizer, max_items=3)

    assert len(result.original_items) == 3
    remaining = manager.get_candidate_context("wf-1")
    # 2 original (uncompressed) + 1 new summary item
    assert len(remaining) == 3
    remaining_types = {i.type for i in remaining}
    assert ContextType.SUMMARY in remaining_types


@pytest.mark.asyncio
async def test_manager_compress_old_context_filters_by_type():
    manager = ContextManager()
    manager.record("conversation text", ContextType.CONVERSATION, workflow_id="wf-1")
    manager.record("tool output text", ContextType.TOOL_RESULT, workflow_id="wf-1")

    result = await manager.compress_old_context(
        "wf-1", fake_summarizer, context_type=ContextType.CONVERSATION
    )

    assert len(result.original_items) == 1
    assert result.original_items[0].type == ContextType.CONVERSATION
    remaining = manager.get_candidate_context("wf-1")
    remaining_types = {i.type for i in remaining}
    assert ContextType.TOOL_RESULT in remaining_types  # untouched


@pytest.mark.asyncio
async def test_manager_compress_old_context_returns_none_when_nothing_to_compress():
    manager = ContextManager()
    result = await manager.compress_old_context("wf-empty", fake_summarizer)
    assert result is None
