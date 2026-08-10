from typing import Optional

import httpx


class FleetClient:
    """Thin reference client for Fleet's HTTP API (REDESIGN.md §64).

    Deliberately minimal — no retry/backoff/streaming smarts, just a
    convenience wrapper so callers don't hand-build requests. The
    higher-level `FleetAgent` convenience wrapper (§65) is explicitly
    deferred (§0.2); this is the only client-side abstraction in scope.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str = "dev-key",
        timeout: float = 120.0,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"x-api-key": api_key, "content-type": "application/json"},
            timeout=timeout,
        )

    async def generate(self, prompt: str, model: Optional[str] = None, **metadata) -> dict:
        """metadata accepts agent_id/workflow_id/request_id/parent_request_id
        (REDESIGN.md §5) — passed straight through as extra JSON fields."""
        payload = {"prompt": prompt, **metadata}
        if model:
            payload["model"] = model
        resp = await self._client.post("/api/v1/generate", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def chat(self, messages: list[dict], model: Optional[str] = None, **metadata) -> dict:
        payload = {"messages": messages, **metadata}
        if model:
            payload["model"] = model
        resp = await self._client.post("/api/v1/chat", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def queued_generate(self, prompt: str, model: Optional[str] = None, **metadata) -> dict:
        payload = {"prompt": prompt, **metadata}
        if model:
            payload["model"] = model
        resp = await self._client.post("/api/v1/queued/generate", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def health(self) -> dict:
        resp = await self._client.get("/api/v1/health")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FleetClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
