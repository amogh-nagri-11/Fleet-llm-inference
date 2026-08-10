import httpx
import pytest

from gateway.metrics import INPUT_TOKENS_TOTAL, OUTPUT_TOKENS_TOTAL
from workers.ollama_client import OllamaClient


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient — OllamaClient constructs one fresh
    per call (`async with httpx.AsyncClient(...) as client`), so this is
    monkeypatched in as the constructor itself."""

    def __init__(self, response_json: dict, status_code: int = 200):
        self._response_json = response_json
        self._status_code = status_code
        self.last_request = None

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        self.last_request = {"url": url, "json": json}
        request = httpx.Request("POST", url)
        return httpx.Response(self._status_code, json=self._response_json, request=request)


def counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


@pytest.mark.asyncio
async def test_generate_returns_response_and_updates_stats(monkeypatch):
    fake = FakeAsyncClient({
        "message": {"content": "hi there"}, "model": "llama3",
        "prompt_eval_count": 10, "eval_count": 5,
    })
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    client = OllamaClient("http://worker-test-1")
    result = await client.generate(model="llama3", prompt="hello")

    assert result["response"] == "hi there"
    assert result["prompt_tokens"] == 10
    assert result["completion_tokens"] == 5
    assert client.stats.total_requests == 1
    assert client.stats.active_requests == 0  # decremented in finally


@pytest.mark.asyncio
async def test_generate_increments_token_metrics(monkeypatch):
    fake = FakeAsyncClient({
        "message": {"content": "hi"}, "model": "llama3",
        "prompt_eval_count": 42, "eval_count": 17,
    })
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    worker_url = "http://worker-metrics-test"
    before_in = counter_value(INPUT_TOKENS_TOTAL, worker_url=worker_url)
    before_out = counter_value(OUTPUT_TOKENS_TOTAL, worker_url=worker_url)

    client = OllamaClient(worker_url)
    await client.generate(model="llama3", prompt="hello")

    assert counter_value(INPUT_TOKENS_TOTAL, worker_url=worker_url) == before_in + 42
    assert counter_value(OUTPUT_TOKENS_TOTAL, worker_url=worker_url) == before_out + 17


@pytest.mark.asyncio
async def test_chat_increments_token_metrics(monkeypatch):
    fake = FakeAsyncClient({
        "message": {"content": "hi"}, "model": "llama3",
        "prompt_eval_count": 8, "eval_count": 3,
    })
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    worker_url = "http://worker-metrics-test-chat"
    before_in = counter_value(INPUT_TOKENS_TOTAL, worker_url=worker_url)
    before_out = counter_value(OUTPUT_TOKENS_TOTAL, worker_url=worker_url)

    client = OllamaClient(worker_url)
    await client.chat(model="llama3", messages=[{"role": "user", "content": "hi"}])

    assert counter_value(INPUT_TOKENS_TOTAL, worker_url=worker_url) == before_in + 8
    assert counter_value(OUTPUT_TOKENS_TOTAL, worker_url=worker_url) == before_out + 3


@pytest.mark.asyncio
async def test_generate_failure_does_not_increment_token_metrics(monkeypatch):
    class FailingClient(FakeAsyncClient):
        async def post(self, url, json):
            raise httpx.ConnectError("connection refused")

    fake = FailingClient({})
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    worker_url = "http://worker-metrics-test-fail"
    before_in = counter_value(INPUT_TOKENS_TOTAL, worker_url=worker_url)
    before_out = counter_value(OUTPUT_TOKENS_TOTAL, worker_url=worker_url)

    client = OllamaClient(worker_url)
    with pytest.raises(RuntimeError):
        await client.generate(model="llama3", prompt="hello")

    assert counter_value(INPUT_TOKENS_TOTAL, worker_url=worker_url) == before_in
    assert counter_value(OUTPUT_TOKENS_TOTAL, worker_url=worker_url) == before_out
    assert client.stats.failures == 1
