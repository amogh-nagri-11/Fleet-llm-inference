import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes import router
from router.load_balancer import load_balancer
from workers.worker_pool import worker_pool
from router.autoscaler import autoscaler
from config import settings

app = FastAPI()
app.include_router(router, prefix="/api/v1")
client = TestClient(app)

HEADERS = {"x-api-key": settings.API_KEY}


class FakeWorker:
    def __init__(self, url="http://fake"):
        self.stats = type("Stats", (), {"url": url})()

    async def generate(self, model, prompt):
        return {"response": "ok", "model": model}

    async def chat(self, model, messages):
        return {"message": {"role": "assistant", "content": "ok"}, "model": model}


# ── Auth ─────────────────────────────────────────────────────

def test_generate_rejects_missing_api_key():
    resp = client.post("/api/v1/generate", json={"prompt": "hi"})
    assert resp.status_code == 401


def test_generate_rejects_wrong_api_key():
    resp = client.post("/api/v1/generate", json={"prompt": "hi"}, headers={"x-api-key": "wrong"})
    assert resp.status_code == 401


def test_workers_rejects_missing_api_key():
    resp = client.get("/api/v1/workers")
    assert resp.status_code == 401


# ── /generate, /chat ─────────────────────────────────────────

def test_generate_success(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    resp = client.post("/api/v1/generate", json={"prompt": "hi"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["response"] == "ok"


def test_generate_returns_503_not_500_when_no_workers_available(monkeypatch):
    def raise_no_workers():
        raise RuntimeError("No available workers — all are unhealthy or circuit open")

    monkeypatch.setattr(load_balancer, "pick_worker", raise_no_workers)

    resp = client.post("/api/v1/generate", json={"prompt": "hi"}, headers=HEADERS)
    assert resp.status_code == 503


def test_chat_returns_503_not_500_when_no_workers_available(monkeypatch):
    def raise_no_workers():
        raise RuntimeError("No available workers — all are unhealthy or circuit open")

    monkeypatch.setattr(load_balancer, "pick_worker", raise_no_workers)

    resp = client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=HEADERS,
    )
    assert resp.status_code == 503


def test_generate_worker_failure_records_failure_and_returns_503(monkeypatch):
    calls = []

    class FailingWorker(FakeWorker):
        async def generate(self, model, prompt):
            raise RuntimeError("Worker http://fake failed: boom")

    monkeypatch.setattr(load_balancer, "pick_worker", lambda: FailingWorker())
    monkeypatch.setattr(load_balancer, "record_failure", lambda url: calls.append(url))

    resp = client.post("/api/v1/generate", json={"prompt": "hi"}, headers=HEADERS)
    assert resp.status_code == 503
    assert calls == ["http://fake"]


def test_chat_success(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    resp = client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=HEADERS,
    )
    assert resp.status_code == 200


# ── /workers, /health, /queue/depth ──────────────────────────

def test_workers_returns_stats(monkeypatch):
    async def fake_health_check_all():
        return None

    monkeypatch.setattr(load_balancer, "health_check_all", fake_health_check_all)
    monkeypatch.setattr(load_balancer, "get_worker_stats", lambda: [{"url": "http://fake"}])

    resp = client.get("/api/v1/workers", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == [{"url": "http://fake"}]


def test_health_reports_queue_depth(monkeypatch):
    async def fake_health_check_all():
        return None

    async def fake_queue_depth():
        return 3

    monkeypatch.setattr(load_balancer, "health_check_all", fake_health_check_all)
    monkeypatch.setattr(worker_pool, "queue_depth", fake_queue_depth)
    monkeypatch.setattr(autoscaler, "_running_worker_count", lambda: 1)
    monkeypatch.setattr(load_balancer, "get_worker_stats", lambda: [])

    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["queue_depth"] == 3
    assert body["worker_count"] == 1


def test_queue_depth_endpoint(monkeypatch):
    async def fake_queue_depth():
        return 7

    monkeypatch.setattr(worker_pool, "queue_depth", fake_queue_depth)

    resp = client.get("/api/v1/queue/depth", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"queue_depth": 7}


# ── /queued/generate ──────────────────────────────────────────

def test_queued_generate_returns_result(monkeypatch):
    async def fake_enqueue(request_id, payload):
        return None

    async def fake_get_result(request_id, timeout=120):
        return {"response": "ok"}

    monkeypatch.setattr(worker_pool, "enqueue", fake_enqueue)
    monkeypatch.setattr(worker_pool, "get_result", fake_get_result)

    resp = client.post("/api/v1/queued/generate", json={"prompt": "hi"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["response"] == "ok"
    assert "request_id" in resp.json()


def test_queued_generate_times_out(monkeypatch):
    async def fake_enqueue(request_id, payload):
        return None

    async def fake_get_result(request_id, timeout=120):
        return None

    monkeypatch.setattr(worker_pool, "enqueue", fake_enqueue)
    monkeypatch.setattr(worker_pool, "get_result", fake_get_result)

    resp = client.post("/api/v1/queued/generate", json={"prompt": "hi"}, headers=HEADERS)
    assert resp.status_code == 504


# ── Agent/workflow metadata (Phase 2) ─────────────────────────

def test_generate_without_metadata_gets_auto_request_id(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    resp = client.post("/api/v1/generate", json={"prompt": "hi"}, headers=HEADERS)
    body = resp.json()
    assert "request_id" in body and body["request_id"]
    assert "agent_id" not in body  # omitted, not sent back as null


def test_generate_echoes_supplied_agent_metadata(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    resp = client.post(
        "/api/v1/generate",
        json={
            "prompt": "hi",
            "agent_id": "coding-agent-42",
            "workflow_id": "workflow-123",
            "request_id": "req-456",
            "parent_request_id": "req-400",
        },
        headers=HEADERS,
    )
    body = resp.json()
    assert body["agent_id"] == "coding-agent-42"
    assert body["workflow_id"] == "workflow-123"
    assert body["request_id"] == "req-456"
    assert body["parent_request_id"] == "req-400"


def test_chat_echoes_supplied_agent_metadata(monkeypatch):
    monkeypatch.setattr(load_balancer, "pick_worker", lambda: FakeWorker())
    monkeypatch.setattr(load_balancer, "record_success", lambda url: None)

    resp = client.post(
        "/api/v1/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "agent_id": "research-agent-1",
            "workflow_id": "workflow-7",
        },
        headers=HEADERS,
    )
    body = resp.json()
    assert body["agent_id"] == "research-agent-1"
    assert body["workflow_id"] == "workflow-7"


def test_queued_generate_uses_supplied_request_id_and_forwards_metadata(monkeypatch):
    captured = {}

    async def fake_enqueue(request_id, payload):
        captured["request_id"] = request_id
        captured["payload"] = payload

    async def fake_get_result(request_id, timeout=120):
        return {"response": "ok"}

    monkeypatch.setattr(worker_pool, "enqueue", fake_enqueue)
    monkeypatch.setattr(worker_pool, "get_result", fake_get_result)

    resp = client.post(
        "/api/v1/queued/generate",
        json={
            "prompt": "hi",
            "agent_id": "batch-agent-9",
            "workflow_id": "workflow-99",
            "request_id": "req-fixed-id",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["request_id"] == "req-fixed-id"
    assert captured["request_id"] == "req-fixed-id"
    assert captured["payload"]["agent_id"] == "batch-agent-9"
    assert captured["payload"]["workflow_id"] == "workflow-99"
