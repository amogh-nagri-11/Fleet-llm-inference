from gateway.metrics import (
    REQUEST_COUNT,
    REQUEST_LATENCY,
    ACTIVE_REQUESTS
)
from fastapi import Request
import time

# ── Middleware ─────────────────────────────────────────────

async def metrics_middleware(request: Request, call_next):
    start = time.monotonic()
    ACTIVE_REQUESTS.inc()

    response = await call_next(request)

    latency = time.monotonic() - start
    ACTIVE_REQUESTS.dec()

    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.url.path,
        status_code=response.status_code
    ).inc()

    REQUEST_LATENCY.labels(
        endpoint=request.url.path
    ).observe(latency)

    return response