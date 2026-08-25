from __future__ import annotations

from typing import Any

from tools.service_health import OpenAIServiceHealthClient


class FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


class FakeAsyncClient:
    request_args: dict[str, Any] = {}

    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request_args = {"url": url, **kwargs}
        return FakeResponse()


async def test_openai_service_health_uses_bearer_without_exposing_key(
    monkeypatch: Any,
) -> None:
    client = OpenAIServiceHealthClient(
        base_url="https://new-api.example/v1/",
        api_key="secret-token",
    )
    fake = FakeAsyncClient()
    monkeypatch.setattr("tools.service_health.httpx.AsyncClient", lambda **kwargs: fake)

    result = await client.health()

    assert result.ok is True
    assert fake.request_args["url"] == "https://new-api.example/v1/models"
    assert fake.request_args["headers"] == {"Authorization": "Bearer secret-token"}
    assert "secret-token" not in result.detail
