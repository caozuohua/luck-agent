from __future__ import annotations

import httpx
import pytest

from llm.openai_compat import LLMRequestError, LLMUnavailableError, OpenAICompatClient


class FakeAsyncClient:
    responses: list[httpx.Response] = []
    requests: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.responses = list(type(self).responses)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> httpx.Response:
        type(self).requests.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def _response(status: int, payload: dict, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers=headers,
        request=httpx.Request("POST", "https://llm.test/v1/chat/completions"),
    )


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch: pytest.MonkeyPatch):
    FakeAsyncClient.responses = []
    FakeAsyncClient.requests = []
    monkeypatch.setattr("llm.openai_compat.httpx.AsyncClient", FakeAsyncClient)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("llm.openai_compat.asyncio.sleep", no_sleep)


async def test_retries_rate_limit_then_returns_text() -> None:
    FakeAsyncClient.responses = [
        _response(429, {"error": "quota temporarily busy"}),
        _response(
            200,
            {"choices": [{"message": {"content": '{"intent":"CHAT","message":"ok"}'}}]},
        ),
    ]
    client = OpenAICompatClient(
        base_url="https://llm.test/v1",
        max_retries=1,
        failure_threshold=2,
    )

    assert await client.generate("system", "task") == '{"intent":"CHAT","message":"ok"}'
    assert len(FakeAsyncClient.requests) == 2


async def test_auth_failure_is_not_retried() -> None:
    FakeAsyncClient.responses = [_response(401, {"error": "invalid key"})]
    client = OpenAICompatClient(base_url="https://llm.test/v1", max_retries=2)

    with pytest.raises(LLMRequestError):
        await client.generate("system", "task")
    assert len(FakeAsyncClient.requests) == 1


async def test_circuit_opens_after_transient_failure() -> None:
    FakeAsyncClient.responses = [_response(503, {"error": "busy"})]
    client = OpenAICompatClient(
        base_url="https://llm.test/v1",
        max_retries=0,
        failure_threshold=1,
        cooldown_seconds=60,
    )

    with pytest.raises(LLMUnavailableError, match="503"):
        await client.generate("system", "task")
    with pytest.raises(LLMUnavailableError, match="circuit open"):
        await client.generate("system", "task")
    assert len(FakeAsyncClient.requests) == 1


async def test_empty_response_is_retried() -> None:
    FakeAsyncClient.responses = [
        _response(200, {"choices": []}),
        _response(200, {"choices": [{"message": {"content": "usable"}}]}),
    ]
    client = OpenAICompatClient(base_url="https://llm.test/v1", max_retries=1)

    assert await client.generate("system", "task") == "usable"
    assert len(FakeAsyncClient.requests) == 2
