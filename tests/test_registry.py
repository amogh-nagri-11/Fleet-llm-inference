import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routes import router
from config import settings
from registry.models import WorkerRecord
from registry.store import RegistryStore, registry_store

app = FastAPI()
app.include_router(router, prefix="/api/v1")
client = TestClient(app)

HEADERS = {"x-api-key": settings.API_KEY}


# HTTP tests below exercise the module-level `registry_store` singleton
# gateway/routes.py imports — this bare TestClient app never runs
# gateway/main.py's lifespan (same pattern test_routes.py already uses
# for load_balancer), so nothing connects it otherwise.
@pytest_asyncio.fixture(autouse=True)
async def _connect_registry_store_singleton():
    if registry_store.redis is None:
        await registry_store.connect()
    yield


# ── RegistryStore (real Redis) ──────────────────────────────

@pytest_asyncio.fixture
async def store():
    s = RegistryStore()
    await s.connect()
    yield s
    # Clean up whatever this test registered so runs don't leak into
    # each other via the shared fleet:registry:workers set.
    for w in await s.list_workers():
        if w.worker_id.startswith("test-"):
            await s.deregister(w.worker_id)
    await s.close()


def make_record(worker_id=None, **overrides) -> WorkerRecord:
    defaults = dict(
        worker_id=worker_id or f"test-{uuid.uuid4().hex[:8]}",
        url="http://192.168.1.42:11434",
        name="test-machine",
        ram_gb=16.0,
        has_gpu=True,
        vram_gb=8.0,
        models=["llama3:8b"],
    )
    defaults.update(overrides)
    return WorkerRecord(**defaults)


@pytest.mark.asyncio
async def test_register_then_get_round_trips(store):
    record = make_record()
    await store.register(record)

    fetched = await store.get_worker(record.worker_id)
    assert fetched is not None
    assert fetched.worker_id == record.worker_id
    assert fetched.url == record.url
    assert fetched.models == ["llama3:8b"]


@pytest.mark.asyncio
async def test_get_worker_returns_none_when_absent(store):
    assert await store.get_worker("test-does-not-exist") is None


@pytest.mark.asyncio
async def test_list_workers_includes_registered_worker(store):
    record = make_record()
    await store.register(record)

    listed_ids = {w.worker_id for w in await store.list_workers()}
    assert record.worker_id in listed_ids


@pytest.mark.asyncio
async def test_reregister_preserves_original_registered_at(store):
    """A machine rebooting and re-registering with the same worker_id
    should not look like a brand-new join."""
    record = make_record()
    first = await store.register(record)

    second = make_record(worker_id=record.worker_id, ram_gb=32.0)
    second_saved = await store.register(second)

    assert second_saved.registered_at == first.registered_at
    assert second_saved.ram_gb == 32.0  # profile fields do update

    fetched = await store.get_worker(record.worker_id)
    assert fetched.registered_at == first.registered_at
    assert fetched.ram_gb == 32.0


@pytest.mark.asyncio
async def test_deregister_removes_worker(store):
    record = make_record()
    await store.register(record)

    removed = await store.deregister(record.worker_id)
    assert removed is True
    assert await store.get_worker(record.worker_id) is None

    listed_ids = {w.worker_id for w in await store.list_workers()}
    assert record.worker_id not in listed_ids


@pytest.mark.asyncio
async def test_deregister_nonexistent_worker_returns_false(store):
    assert await store.deregister("test-never-registered") is False


# ── HTTP endpoints ───────────────────────────────────────────

def unique_worker_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def test_register_worker_rejects_missing_api_key():
    resp = client.post("/api/v1/registry/register", json={
        "worker_id": unique_worker_id(), "url": "http://x:11434", "ram_gb": 16, "has_gpu": False,
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_register_worker_success_and_appears_in_listing():
    worker_id = unique_worker_id()
    payload = {
        "worker_id": worker_id,
        "url": "http://192.168.1.50:11434",
        "name": "victus-wsl",
        "ram_gb": 32.0,
        "has_gpu": True,
        "vram_gb": 12.0,
        "models": ["llama3:8b", "mistral:7b"],
    }

    resp = client.post("/api/v1/registry/register", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["worker_id"] == worker_id
    assert body["models"] == ["llama3:8b", "mistral:7b"]

    listing = client.get("/api/v1/registry", headers=HEADERS).json()
    assert any(w["worker_id"] == worker_id for w in listing)

    client.delete(f"/api/v1/registry/{worker_id}", headers=HEADERS)  # cleanup


@pytest.mark.asyncio
async def test_register_worker_defaults_name_to_worker_id_when_omitted():
    worker_id = unique_worker_id()
    resp = client.post("/api/v1/registry/register", json={
        "worker_id": worker_id, "url": "http://x:11434", "ram_gb": 16, "has_gpu": False,
    }, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["name"] == worker_id

    client.delete(f"/api/v1/registry/{worker_id}", headers=HEADERS)  # cleanup


def test_list_registered_workers_rejects_missing_api_key():
    resp = client.get("/api/v1/registry")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_deregister_worker_success():
    worker_id = unique_worker_id()
    client.post("/api/v1/registry/register", json={
        "worker_id": worker_id, "url": "http://x:11434", "ram_gb": 16, "has_gpu": False,
    }, headers=HEADERS)

    resp = client.delete(f"/api/v1/registry/{worker_id}", headers=HEADERS)
    assert resp.status_code == 200

    listing = client.get("/api/v1/registry", headers=HEADERS).json()
    assert not any(w["worker_id"] == worker_id for w in listing)


@pytest.mark.asyncio
async def test_deregister_unknown_worker_returns_404():
    resp = client.delete("/api/v1/registry/test-never-existed", headers=HEADERS)
    assert resp.status_code == 404


def test_deregister_worker_rejects_missing_api_key():
    resp = client.delete("/api/v1/registry/test-anything")
    assert resp.status_code == 401
