import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from context.models import ContextItem, ContextType
from context.store import ContextStore
from gateway.metrics import (
    AGENT_REQUEST_COUNT,
    AGENT_WORKFLOW_FAILURES,
    CONTEXT_CAPACITY_REJECTIONS,
    CONTEXT_ITEMS_RECORDED,
    CONTEXT_TOKENS,
)
from gateway.routes import router
from router.load_balancer import LoadBalancer, NoCapacityError, load_balancer

app = FastAPI()
app.include_router(router, prefix="/api/v1")
client = TestClient(app)
HEADERS = {"x-api-key": "dev-key"}


def wf_id() -> str:
    return f"test-metrics-wf-{uuid.uuid4()}"


class FakeWorker:
    stats = type("Stats", (), {"url": "http://fake"})()

    async def generate(self, model, prompt):
        return {"response": "ok", "model": model}

    async def chat(self, model, messages):
        return {"message": {"content": "ok"}, "model": model}


def counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get() if labels else counter._value.get()


def histogram_sample_count(histogram) -> tuple[float, float]:
    total_sum = total_count = 0.0
    for metric in histogram.collect():
        for s in metric.samples:
            if s.name.endswith("_sum"):
                total_sum = s.value
            elif s.name.endswith("_count"):
                total_count = s.value
    return total_sum, total_count


# ── AGENT_REQUEST_COUNT ────────────────────────────────────────

def test_agent_request_count_increments_with_correct_labels(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    before_with = counter_value(AGENT_REQUEST_COUNT, endpoint="/generate", has_agent_id="True")
    before_without = counter_value(AGENT_REQUEST_COUNT, endpoint="/generate", has_agent_id="False")

    client.post("/api/v1/generate", json={"prompt": "hi", "agent_id": "a1"}, headers=HEADERS)
    client.post("/api/v1/generate", json={"prompt": "hi"}, headers=HEADERS)

    assert counter_value(AGENT_REQUEST_COUNT, endpoint="/generate", has_agent_id="True") == before_with + 1
    assert counter_value(AGENT_REQUEST_COUNT, endpoint="/generate", has_agent_id="False") == before_without + 1


# ── CONTEXT_TOKENS ───────────────────────────────────────────

def test_context_tokens_observed_only_for_agent_aware_requests(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda **kwargs: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    sum_before, count_before = histogram_sample_count(CONTEXT_TOKENS)

    client.post("/api/v1/generate", json={"prompt": "hi"}, headers=HEADERS)  # no workflow_id
    sum_after_plain, count_after_plain = histogram_sample_count(CONTEXT_TOKENS)
    assert count_after_plain == count_before  # not observed

    client.post("/api/v1/generate", json={"prompt": "hi", "workflow_id": wf_id()}, headers=HEADERS)
    sum_after, count_after = histogram_sample_count(CONTEXT_TOKENS)
    assert count_after == count_before + 1
    assert sum_after > sum_before


# ── AGENT_WORKFLOW_FAILURES ────────────────────────────────────

def test_agent_workflow_failures_only_counted_for_agent_aware_requests(monkeypatch):
    def raise_no_workers(**kwargs):
        raise RuntimeError("No available workers — all are unhealthy or circuit open")

    monkeypatch.setattr(load_balancer, "pick_worker", raise_no_workers)

    before = counter_value(AGENT_WORKFLOW_FAILURES, endpoint="/generate")

    client.post("/api/v1/generate", json={"prompt": "hi"}, headers=HEADERS)  # plain caller
    assert counter_value(AGENT_WORKFLOW_FAILURES, endpoint="/generate") == before

    client.post("/api/v1/generate", json={"prompt": "hi", "workflow_id": wf_id()}, headers=HEADERS)
    assert counter_value(AGENT_WORKFLOW_FAILURES, endpoint="/generate") == before + 1


# ── CONTEXT_ITEMS_RECORDED ─────────────────────────────────────

def test_context_items_recorded_increments_on_store_add():
    before = CONTEXT_ITEMS_RECORDED._value.get()
    store = ContextStore()
    store.add(ContextItem(type=ContextType.CONVERSATION, content="hi", workflow_id="wf-x"))
    assert CONTEXT_ITEMS_RECORDED._value.get() == before + 1


# ── CONTEXT_CAPACITY_REJECTIONS + NoCapacityError ─────────────

def test_no_capacity_error_is_a_runtime_error_subclass():
    assert issubclass(NoCapacityError, RuntimeError)


def test_pick_worker_raises_no_capacity_error_specifically_on_capacity_rejection():
    lb = LoadBalancer(["http://a"])
    lb.workers[0].max_context_tokens = 100

    with pytest.raises(NoCapacityError):
        lb.pick_worker(context_tokens=99999)


def test_pick_worker_raises_plain_runtime_error_when_no_healthy_workers():
    lb = LoadBalancer(["http://a"])
    lb.workers[0].stats.is_healthy = False

    with pytest.raises(RuntimeError) as exc_info:
        lb.pick_worker()
    assert not isinstance(exc_info.value, NoCapacityError)


def test_context_capacity_rejections_increments_only_on_capacity_rejection():
    before = CONTEXT_CAPACITY_REJECTIONS._value.get()

    lb = LoadBalancer(["http://a"])
    lb.workers[0].stats.is_healthy = False
    with pytest.raises(RuntimeError):
        lb.pick_worker()
    assert CONTEXT_CAPACITY_REJECTIONS._value.get() == before  # unrelated failure, not counted

    lb2 = LoadBalancer(["http://b"])
    lb2.workers[0].max_context_tokens = 100
    with pytest.raises(NoCapacityError):
        lb2.pick_worker(context_tokens=99999)
    assert CONTEXT_CAPACITY_REJECTIONS._value.get() == before + 1
