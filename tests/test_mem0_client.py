from __future__ import annotations

from typing import Any

import httpx

from tools.mem0_client import Mem0Client


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "request failed",
                request=httpx.Request("GET", "http://mem0"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> Any:
        return self._payload


class FakeAsyncClient:
    requests: list[dict[str, Any]] = []
    responses: list[FakeResponse] = []

    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


async def test_mem0_health_and_search(monkeypatch: Any) -> None:
    FakeAsyncClient.requests = []
    FakeAsyncClient.responses = [
        FakeResponse({"openapi": "3.1.0"}),
        FakeResponse({"results": [{"id": "m1", "memory": "AWS"}]}),
    ]
    monkeypatch.setattr("tools.mem0_client.httpx.AsyncClient", FakeAsyncClient)
    client = Mem0Client(
        base_url="http://mem0:8888/",
        api_key="secret",
        user_id="u1",
        agent_id="a1",
    )

    health = await client.health()
    results = await client.search("AWS")

    assert health.ok is True
    assert results[0]["memory"] == "AWS"
    assert FakeAsyncClient.requests[1]["headers"] == {"X-API-Key": "secret"}
    assert FakeAsyncClient.requests[1]["json"]["filters"] == {
        "user_id": "u1",
        "agent_id": "a1",
    }


async def test_mem0_list_uses_configured_scope_and_limit(monkeypatch: Any) -> None:
    FakeAsyncClient.requests = []
    FakeAsyncClient.responses = [
        FakeResponse({"memories": [{"id": "m1", "memory": "AWS"}]}),
    ]
    monkeypatch.setattr("tools.mem0_client.httpx.AsyncClient", FakeAsyncClient)
    client = Mem0Client(
        base_url="http://mem0:8888",
        api_key="secret",
        user_id="u1",
        agent_id="a1",
    )

    results = await client.list_memories(limit=99)

    assert results[0]["id"] == "m1"
    request = FakeAsyncClient.requests[0]
    assert request["params"] == {
        "user_id": "u1",
        "agent_id": "a1",
        "page": 1,
        "page_size": 20,
    }
    assert request["headers"] == {"X-API-Key": "secret"}


async def test_mem0_lark_user_scope_overrides_configured_user(monkeypatch: Any) -> None:
    FakeAsyncClient.requests = []
    FakeAsyncClient.responses = [
        FakeResponse({"results": [{"id": "m1", "memory": "private"}]}),
    ]
    monkeypatch.setattr("tools.mem0_client.httpx.AsyncClient", FakeAsyncClient)
    client = Mem0Client(
        base_url="http://mem0:8888",
        api_key="secret",
        user_id="personal",
        agent_id="a1",
        scope_mode="lark_user",
    )

    results = await client.search("private", actor_id="ou_alice")

    assert results[0]["id"] == "m1"
    assert FakeAsyncClient.requests[0]["json"]["filters"]["user_id"] == "ou_alice"
    assert client.scope_label("ou_alice") == "mode=lark_user · user=ou_alice · agent=a1"
    assert client.effective_user_id("default") == "personal"


async def test_mem0_lark_user_delete_requires_visible_memory(monkeypatch: Any) -> None:
    FakeAsyncClient.requests = []
    FakeAsyncClient.responses = [
        FakeResponse({"results": [{"id": "m1", "memory": "private"}]}),
        FakeResponse({}),
    ]
    monkeypatch.setattr("tools.mem0_client.httpx.AsyncClient", FakeAsyncClient)
    client = Mem0Client(
        base_url="http://mem0:8888",
        api_key="secret",
        user_id="personal",
        agent_id="a1",
        scope_mode="lark_user",
    )

    await client.search("private", actor_id="ou_alice")
    await client.delete("m1", actor_id="ou_alice")

    assert FakeAsyncClient.requests[-1]["url"].endswith("/memories/m1")

    try:
        await client.delete("m2", actor_id="ou_alice")
    except Exception as exc:
        assert "当前用户 scope" in str(exc)
    else:
        raise AssertionError("cross-scope delete should be rejected")


async def test_mem0_smoke_deletes_only_added_ids(monkeypatch: Any) -> None:
    FakeAsyncClient.requests = []
    FakeAsyncClient.responses = [
        FakeResponse({"results": [{"id": "m1", "memory": "marker"}]}),
        FakeResponse({"results": [{"id": "m1", "memory": "marker"}]}),
        FakeResponse({}),
    ]
    monkeypatch.setattr("tools.mem0_client.httpx.AsyncClient", FakeAsyncClient)
    client = Mem0Client(base_url="http://mem0:8888", api_key="secret")

    result = await client.smoke()

    assert result.added == 1
    assert result.found == 1
    assert result.deleted == 1
    assert result.cleanup_confirmed is True
    assert FakeAsyncClient.requests[-1]["url"].endswith("/memories/m1")
