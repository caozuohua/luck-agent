from __future__ import annotations

import os

import httpx
import pytest

from tools.web_search import WebSearchTool
from tools.registry import ToolRegistry


class FakeResponse:
    def __init__(self, payload: dict, status_error: Exception | None = None) -> None:
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self) -> None:
        if self.status_error:
            raise self.status_error

    def json(self) -> dict:
        return self.payload


class FakeAsyncClient:
    calls: list[dict] = []
    response = FakeResponse(
        {
            "organic": [
                {
                    "title": "Result A",
                    "link": "https://example.com/a",
                    "snippet": "Snippet A",
                },
                {
                    "title": "Result B",
                    "link": "https://example.com/b",
                    "snippet": "Snippet B",
                },
            ]
        }
    )

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, headers: dict, json: dict) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": self.timeout})
        return self.response


@pytest.mark.asyncio
async def test_web_search_returns_serper_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.calls = []
    FakeAsyncClient.response = FakeResponse(
        {
            "organic": [
                {
                    "title": "Result A",
                    "link": "https://example.com/a",
                    "snippet": "Snippet A",
                },
                {
                    "title": "Result B",
                    "link": "https://example.com/b",
                    "snippet": "Snippet B",
                },
            ]
        }
    )

    result = await WebSearchTool().search("latest python", num_results=2)

    assert result.status == "ok"
    assert result.data == [
        {"title": "Result A", "url": "https://example.com/a", "snippet": "Snippet A"},
        {"title": "Result B", "url": "https://example.com/b", "snippet": "Snippet B"},
    ]
    assert FakeAsyncClient.calls[0]["headers"]["X-API-KEY"] == "test-key"
    assert FakeAsyncClient.calls[0]["json"]["num"] == 2
    assert FakeAsyncClient.calls[0]["timeout"] == 10


@pytest.mark.asyncio
async def test_web_search_http_error_returns_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse({}, httpx.HTTPStatusError("bad", request=None, response=None))

    result = await WebSearchTool().search("news")

    assert result.status == "error"
    assert "bad" in result.error


@pytest.mark.asyncio
async def test_web_search_missing_api_key_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    result = await WebSearchTool().search("news")

    assert result.status == "error"
    assert "SERPER_API_KEY" in result.error


def test_web_search_docstring_contains_l1_and_l2_guidance() -> None:
    doc = WebSearchTool().docstring("need realtime external knowledge")

    assert "query" in doc
    assert "num_results" in doc
    assert "{title, url, snippet}" in doc
    assert "实时信息" in doc or "external knowledge" in doc


def test_builtin_registry_includes_web_search_and_shell() -> None:
    registry = ToolRegistry()
    registry.register_builtin_tools()

    assert "web_search" in registry.names()
    assert "shell" in registry.names()
