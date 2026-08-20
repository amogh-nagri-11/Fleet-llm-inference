from fastapi import FastAPI
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from gateway.routes import router
from gateway.middleware import metrics_middleware
from router.load_balancer import load_balancer
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from contextlib import asynccontextmanager
from router.health_checker import health_checker
from workers.worker_pool import worker_pool
from router.autoscaler import autoscaler
from memory.manager import memory_manager
from registry.store import registry_store
from config import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting LLM Inference Engine...")

    # Workers
    await load_balancer.health_check_all()
    stats = load_balancer.get_worker_stats()
    healthy = sum(1 for w in stats if w["healthy"])
    print(f"Workers online: {healthy}/{len(stats)}")

    #Redis + queue
    await worker_pool.connect()
    worker_pool.start()

    await autoscaler.connect()
    autoscaler.start()

    await registry_store.connect()
    print(f"[Registry] Connected to Redis at {settings.REDIS_HOST}:{settings.REDIS_PORT}")

    # Memory storage (PostgreSQL, REDESIGN.md §18) — was built and tested
    # in Phase 6/7 but never actually connected here, so memory_manager
    # was unreachable from the live app until now.
    await memory_manager.store.connect(
        host=settings.MEMORY_DB_HOST,
        port=settings.MEMORY_DB_PORT,
        database=settings.MEMORY_DB_NAME,
        user=settings.MEMORY_DB_USER,
        password=settings.MEMORY_DB_PASSWORD or None,
    )
    print(f"[MemoryManager] Connected to Postgres at {settings.MEMORY_DB_HOST}:{settings.MEMORY_DB_PORT}")

    # background health check
    health_checker.start()

    yield

    health_checker.stop()
    autoscaler.stop()
    await worker_pool.stop()
    await registry_store.close()
    await memory_manager.store.close()

    print("Shutting down...")


app = FastAPI(
    title="LLM Inference Engine",
    description="Distributed inference gateway for Ollama workers",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(BaseHTTPMiddleware, dispatch=metrics_middleware)
app.include_router(router, prefix="/api/v1")


@app.get("/")
async def root():
    return {
        "service": "LLM Inference Engine",
        "version": "0.2.0",
        "docs": "/docs",
        "metrics": "/metrics",
    }


@app.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )