import asyncio
from typing import List, Optional
from workers.ollama_client import OllamaClient
from router.circuit_breaker import CircuitBreaker
from config import settings


class LoadBalancer:
    def __init__(self, worker_urls: List[str]):
        self.workers = [OllamaClient(url, timeout=settings.REQUEST_TIMEOUT) for url in worker_urls]
        self.breakers = {url: CircuitBreaker(url) for url in worker_urls}
        self._rr_index = 0

    def _available_workers(self, context_tokens: Optional[int] = None) -> List[OllamaClient]:
        healthy = [
            w for w in self.workers
            if w.stats.is_healthy and not self.breakers[w.stats.url].is_open()
        ]
        if context_tokens is None:
            return healthy
        # Context-aware routing (REDESIGN.md §24/§41) — a worker whose
        # context capacity can't hold the request is hard-ineligible, not
        # just deprioritized. Workers that are merely busy stay eligible;
        # this only filters on hard capacity.
        return [w for w in healthy if w.max_context_tokens >= context_tokens]

    def pick_worker(self, context_tokens: Optional[int] = None) -> OllamaClient:
        available = self._available_workers(context_tokens)
        if not available:
            if context_tokens is not None and self._available_workers():
                raise RuntimeError(
                    f"No available workers can handle {context_tokens} tokens of context "
                    f"(max capacity across healthy workers is lower)"
                )
            raise RuntimeError("No available workers — all are unhealthy or circuit open")

        strategy = settings.ROUTING_STRATEGY

        if strategy == "round_robin":
            worker = available[self._rr_index % len(available)]
            self._rr_index += 1
            return worker

        elif strategy == "least_latency":
            return min(available, key=lambda w: w.stats.avg_latency)

        elif strategy == "queue_depth":
            return min(available, key=lambda w: w.stats.active_requests)

        return available[0]

    def has_worker(self, url: str) -> bool:
        return any(w.stats.url == url for w in self.workers)

    def add_worker(self, url: str) -> bool:
        """Register a worker at runtime (used by the autoscaler fallback).
        Idempotent — returns False if the worker is already registered."""
        if self.has_worker(url):
            return False
        self.workers.append(OllamaClient(url, timeout=settings.REQUEST_TIMEOUT))
        self.breakers[url] = CircuitBreaker(url)
        return True

    def remove_worker(self, url: str) -> bool:
        """Deregister a worker at runtime. Returns False if it wasn't present."""
        before = len(self.workers)
        self.workers = [w for w in self.workers if w.stats.url != url]
        self.breakers.pop(url, None)
        return len(self.workers) < before

    def record_success(self, worker_url: str):
        self.breakers[worker_url].record_success()

    def record_failure(self, worker_url: str):
        self.breakers[worker_url].record_failure()

    async def health_check_all(self):
        await asyncio.gather(*[w.health_check() for w in self.workers])

    def get_worker_stats(self) -> list:
        return [
            {
                "url": w.stats.url,
                "healthy": w.stats.is_healthy,
                "active_requests": w.stats.active_requests,
                "total_requests": w.stats.total_requests,
                "avg_latency_ms": round(w.stats.avg_latency * 1000, 2),
                "failures": w.stats.failures,
                "circuit_state": self.breakers[w.stats.url].status,
            }
            for w in self.workers
        ]


load_balancer = LoadBalancer(settings.WORKER_URLS)