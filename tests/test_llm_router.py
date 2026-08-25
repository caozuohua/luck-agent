from __future__ import annotations

import httpx
import pytest

from llm.openai_compat import (
    LLMRequestError,
    LLMUnavailableError,
    OpenAICompatClient,
)
from llm.router import LLMProviderRouter
from settings import _load_llm_providers


class _FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        self.calls += 1
        raise self.error

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        self.calls += 1
        raise self.error


class _WorkingClient:
    model = "backup-model"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        self.calls += 1
        return "backup answer"

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        self.calls += 1
        return "repaired"


async def test_router_falls_back_after_quota_failure() -> None:
    primary = _FailingClient(
        LLMUnavailableError("quota exhausted", kind="quota", provider="primary")
    )
    backup = _WorkingClient()
    router = LLMProviderRouter([("primary", primary), ("backup", backup)])

    assert await router.generate("system", "task") == "backup answer"
    assert primary.calls == 1
    assert backup.calls == 1
    assert router.active_provider == "backup"


async def test_router_falls_back_for_provider_auth_but_not_bad_request() -> None:
    auth_primary = _FailingClient(
        LLMRequestError("invalid key", kind="auth", status_code=401, provider="primary")
    )
    backup = _WorkingClient()
    router = LLMProviderRouter([("primary", auth_primary), ("backup", backup)])

    assert await router.generate("system", "task") == "backup answer"

    bad_request = _FailingClient(
        LLMRequestError("invalid request", kind="request", status_code=400, provider="primary")
    )
    router = LLMProviderRouter([("primary", bad_request), ("backup", _WorkingClient())])
    with pytest.raises(LLMRequestError, match="invalid request"):
        await router.generate("system", "task")


def test_provider_settings_preserve_legacy_primary_and_load_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "primary-key")
    monkeypatch.setenv("LLM_MODEL", "primary-model")
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "primary, backup")
    monkeypatch.setenv("LLM_PROVIDER_BACKUP_BASE_URL", "https://backup.example/v1")
    monkeypatch.setenv("LLM_PROVIDER_BACKUP_API_KEY", "backup-key")
    monkeypatch.setenv("LLM_PROVIDER_BACKUP_MODEL", "backup-model")

    providers = _load_llm_providers()

    assert [provider.name for provider in providers] == ["primary", "backup"]
    assert providers[0].base_url == "https://primary.example/v1"
    assert providers[0].model == "primary-model"
    assert providers[1].api_key == "backup-key"
    assert providers[1].model == "backup-model"


class _FakeAsyncClient:
    requests: list[dict] = []
    responses: list[httpx.Response] = []

    def __init__(self, **kwargs) -> None:
        self.responses = list(type(self).responses)

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> httpx.Response:
        type(self).requests.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://llm.test/v1/chat/completions"),
    )


@pytest.mark.asyncio
async def test_quota_failure_opens_long_lived_provider_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncClient.requests = []
    _FakeAsyncClient.responses = [_response(402, {"error": "billing required"})]
    monkeypatch.setattr("llm.openai_compat.httpx.AsyncClient", _FakeAsyncClient)
    client = OpenAICompatClient(
        base_url="https://llm.test/v1",
        max_retries=2,
        quota_cooldown_seconds=3600,
    )

    with pytest.raises(LLMUnavailableError, match="quota") as first:
        await client.generate("system", "task")
    with pytest.raises(LLMUnavailableError, match="circuit open") as second:
        await client.generate("system", "task")

    assert first.value.kind == "quota"
    assert second.value.kind == "quota"
    assert len(_FakeAsyncClient.requests) == 1
