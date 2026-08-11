import json

import httpx
import pytest

from client.fleet_client import FleetClient


def make_client(handler) -> FleetClient:
    client = FleetClient(base_url="http://fleet.test", api_key="test-key")
    client._client = httpx.AsyncClient(
        base_url="http://fleet.test",
        headers={"x-api-key": "test-key", "content-type": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_generate_posts_to_correct_endpoint_with_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "ok"})

    client = make_client(handler)
    result = await client.generate("hello")

    assert captured["url"] == "http://fleet.test/api/v1/generate"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["body"] == {"prompt": "hello"}
    assert result == {"response": "ok"}
    await client.close()


@pytest.mark.asyncio
async def test_generate_includes_model_and_metadata():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "ok"})

    client = make_client(handler)
    await client.generate("hello", model="llama3:latest", agent_id="a1", workflow_id="wf-1")

    assert captured["body"] == {
        "prompt": "hello", "agent_id": "a1", "workflow_id": "wf-1", "model": "llama3:latest"
    }
    await client.close()


@pytest.mark.asyncio
async def test_chat_posts_messages():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "hi"}})

    client = make_client(handler)
    messages = [{"role": "user", "content": "hi"}]
    result = await client.chat(messages, workflow_id="wf-1")

    assert captured["url"] == "http://fleet.test/api/v1/chat"
    assert captured["body"] == {"messages": messages, "workflow_id": "wf-1"}
    assert result == {"message": {"content": "hi"}}
    await client.close()


@pytest.mark.asyncio
async def test_queued_generate_posts_to_correct_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://fleet.test/api/v1/queued/generate"
        return httpx.Response(200, json={"request_id": "r1", "response": "ok"})

    client = make_client(handler)
    result = await client.queued_generate("hi")
    assert result["request_id"] == "r1"
    await client.close()


@pytest.mark.asyncio
async def test_health_is_a_get_request():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://fleet.test/api/v1/health"
        return httpx.Response(200, json={"status": "ok"})

    client = make_client(handler)
    result = await client.health()
    assert result == {"status": "ok"}
    await client.close()


@pytest.mark.asyncio
async def test_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "no workers"})

    client = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.generate("hi")
    await client.close()


@pytest.mark.asyncio
async def test_context_manager_closes_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async with make_client(handler) as client:
        await client.health()
    assert client._client.is_closed
