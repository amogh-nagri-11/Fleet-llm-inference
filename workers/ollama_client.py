import httpx
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional
from config import settings
from gateway.metrics import (
    WORKER_ACTIVE_REQUESTS,
    WORKER_REQUEST_COUNT,
    WORKER_LATENCY,
    WORKER_HEALTH,
    INPUT_TOKENS_TOTAL,
    OUTPUT_TOKENS_TOTAL,
)

# WORKER_ACTIVE_REQUESTS = Gauge(
#     "llm_worker_active_requests",
#     "Active requests per worker",
#     ["worker_url"]
# )

# WORKER_REQUEST_COUNT = Counter(
#     "llm_worker_requests_total",
#     "Total requests per worker",
#     ["worker_url", "status"]  # status: success | failure
# )

# WORKER_LATENCY = Histogram(
#     "llm_worker_latency_seconds",
#     "Per-worker response latency",
#     ["worker_url"],
#     buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
# )

# WORKER_HEALTH = Gauge(
#     "llm_worker_healthy",
#     "Worker health status (1=healthy, 0=unhealthy)",
#     ["worker_url"]
# )

@dataclass
class WorkerStats:
    url: str
    active_requests: int = 0
    total_requests: int = 0
    total_latency: float = 0.0
    failures: int = 0
    is_healthy: bool = True

    @property
    def avg_latency(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency / self.total_requests


class OllamaClient:
    def __init__(self, base_url: str, timeout: int = 120, max_context_tokens: Optional[int] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Context-aware routing (REDESIGN.md §24/§41) — how many tokens of
        # context this worker can accept. Defaults from settings rather
        # than a hardcoded literal so it stays configurable without
        # touching call sites.
        self.max_context_tokens = (
            max_context_tokens if max_context_tokens is not None
            else settings.WORKER_MAX_CONTEXT_TOKENS
        )
        self.stats = WorkerStats(url=base_url)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                self.stats.is_healthy = response.status_code == 200
                return self.stats.is_healthy
        except Exception:
            self.stats.is_healthy = False
            return False

    async def generate(self, model: str, prompt: str, stream: bool = False) -> dict:
        start = time.monotonic()
        self.stats.active_requests += 1
        WORKER_ACTIVE_REQUESTS.labels(worker_url=self.base_url).inc()   

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                    }
                )
                response.raise_for_status()
                result = response.json()

                # Update stats
                latency = time.monotonic() - start
                WORKER_LATENCY.labels(worker_url=self.base_url).observe(latency)
                WORKER_REQUEST_COUNT.labels(worker_url=self.base_url, status="success").inc()

                self.stats.total_requests += 1
                self.stats.total_latency += latency
                self.stats.failures = 0  # reset on success

                prompt_tokens = result.get("prompt_eval_count", 0)
                completion_tokens = result.get("eval_count", 0)
                INPUT_TOKENS_TOTAL.labels(worker_url=self.base_url).inc(prompt_tokens)
                OUTPUT_TOKENS_TOTAL.labels(worker_url=self.base_url).inc(completion_tokens)

                return {
                    "response": result.get("message", {}).get("content", ""),
                    "model": result.get("model"),
                    "worker_url": self.base_url,
                    "latency_ms": round(latency * 1000, 2),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
        except Exception as e:
            self.stats.failures += 1
            WORKER_REQUEST_COUNT.labels(worker_url=self.base_url, status="failure").inc()
            raise RuntimeError(f"Worker {self.base_url} failed: {str(e)}")
        finally:
            self.stats.active_requests -= 1
            WORKER_ACTIVE_REQUESTS.labels(worker_url=self.base_url).dec()

    async def chat(self, model: str, messages: list) -> dict:
        start = time.monotonic()
        self.stats.active_requests += 1
        WORKER_ACTIVE_REQUESTS.labels(worker_url=self.base_url).inc()

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                    }
                )
                response.raise_for_status()
                result = response.json()

                latency = time.monotonic() - start
                WORKER_LATENCY.labels(worker_url=self.base_url).observe(latency)
                WORKER_REQUEST_COUNT.labels(worker_url=self.base_url, status="success").inc()

                self.stats.total_requests += 1
                self.stats.total_latency += latency
                self.stats.failures = 0

                prompt_tokens = result.get("prompt_eval_count", 0)
                completion_tokens = result.get("eval_count", 0)
                INPUT_TOKENS_TOTAL.labels(worker_url=self.base_url).inc(prompt_tokens)
                OUTPUT_TOKENS_TOTAL.labels(worker_url=self.base_url).inc(completion_tokens)

                return {
                    "message": result.get("message", {}),
                    "model": result.get("model"),
                    "worker_url": self.base_url,
                    "latency_ms": round(latency * 1000, 2),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
        except Exception as e:
            self.stats.failures += 1
            WORKER_REQUEST_COUNT.labels(worker_url=self.base_url, status="failure").inc()
            raise RuntimeError(f"Worker {self.base_url} failed: {str(e)}")
        finally:
            self.stats.active_requests -= 1
            WORKER_ACTIVE_REQUESTS.labels(worker_url=self.base_url).dec()