from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import quote

import httpx


VALID_PKB_TYPES = {"fact", "idea", "task", "question", "code"}
_RETRY_DELAYS = (0.25, 0.5)
_pkb_client: PkbClient | None = None
_ERRORS = {
    400: ("invalid_arguments", "PKB 请求参数错误", False),
    401: ("authentication_failed", "PKB 认证失败", False),
    404: ("not_found", "PKB 笔记不存在或已删除", False),
    409: ("duplicate", "PKB 中已存在相同内容", False),
}


def _env_timeout_ms() -> int:
    raw_value = os.getenv("PKB_TIMEOUT_MS", "10000").strip()
    try:
        value = int(raw_value)
    except ValueError:
        return 10000
    return value if value > 0 else 10000


def _error(
    status: int | None,
    code: str,
    message: str,
    retryable: bool,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "code": code,
        "error": message,
        "retryable": retryable,
    }


def _validate_type(note_type: str | None) -> None:
    if note_type is not None and note_type not in VALID_PKB_TYPES:
        raise ValueError(f"Invalid PKB type: {note_type}")


def _note_path(note_id: str) -> str:
    if not note_id:
        raise ValueError("note_id is required")
    return f"/api/pkb/{quote(note_id, safe='')}"


class PkbClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_secret: str | None = None,
        timeout_ms: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        configured_url = base_url if base_url is not None else os.getenv("PKB_BASE_URL", "")
        configured_secret = (
            api_secret if api_secret is not None else os.getenv("PKB_API_SECRET", "")
        )
        self.base_url = configured_url.strip().rstrip("/")
        self.api_secret = configured_secret.strip()
        self.timeout_ms = timeout_ms if timeout_ms is not None else _env_timeout_ms()
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be greater than zero")
        self.transport = transport
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_ms / 1000),
            transport=self.transport,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, "
            f"timeout_ms={self.timeout_ms!r})"
        )

    async def __aenter__(self) -> PkbClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        if not self.base_url or (authenticated and not self.api_secret):
            return _error(
                None,
                "configuration_error",
                "PKB 配置不完整",
                False,
            )

        headers: dict[str, str] = {}
        if authenticated:
            headers["x-api-secret"] = self.api_secret
            headers["Content-Type"] = "application/json"

        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                response = await self._client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                )
            except httpx.TransportError:
                if attempt < len(_RETRY_DELAYS):
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                    continue
                return _error(None, "unavailable", "PKB 暂时不可用", True)

            if response.status_code in (500, 503):
                if attempt < len(_RETRY_DELAYS):
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                    continue
                return _error(
                    response.status_code,
                    "unavailable",
                    "PKB 暂时不可用",
                    True,
                )

            if response.status_code in _ERRORS:
                code, message, retryable = _ERRORS[response.status_code]
                return _error(response.status_code, code, message, retryable)

            if response.is_error:
                return _error(
                    response.status_code,
                    "request_failed",
                    "PKB 请求失败",
                    False,
                )

            try:
                result = response.json()
            except ValueError:
                return _error(
                    response.status_code,
                    "protocol_error",
                    "PKB 返回了无效响应",
                    False,
                )
            if not isinstance(result, dict):
                return _error(
                    response.status_code,
                    "protocol_error",
                    "PKB 返回了无效响应",
                    False,
                )
            return result

        return _error(None, "unavailable", "PKB 暂时不可用", True)

    async def save(
        self,
        content: str,
        *,
        source: str = "luck-agent",
        note_type: str = "fact",
        topics: list[str] | None = None,
    ) -> dict[str, Any]:
        _validate_type(note_type)
        return await self._request(
            "POST",
            "/api/pkb",
            json_body={
                "content": content,
                "source": source,
                "type": note_type,
                "topics": topics or [],
            },
        )

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        source: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "action": "search",
        }
        if source is not None:
            body["source"] = source
        return await self._request("POST", "/api/pkb/search", json_body=body)

    async def get(self, note_id: str) -> dict[str, Any]:
        return await self._request("GET", _note_path(note_id))

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        note_type: str | None = None,
        topics: list[str] | None = None,
        from_: str | None = None,
        to: str | None = None,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        _validate_type(note_type)
        params: dict[str, Any] = {
            "limit": min(100, max(1, limit)),
            "offset": offset,
        }
        if note_type is not None:
            params["type"] = note_type
        if topics:
            params["topics"] = ",".join(topics)
        if from_ is not None:
            params["from"] = from_
        if to is not None:
            params["to"] = to
        if include_deleted:
            params["include_deleted"] = "true"
        return await self._request("GET", "/api/pkb/list", params=params)

    async def update(
        self,
        note_id: str,
        *,
        content: str | None = None,
        note_type: str | None = None,
        topics: list[str] | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        _validate_type(note_type)
        values = {
            "content": content,
            "type": note_type,
            "topics": topics,
            "summary": summary,
        }
        body = {key: value for key, value in values.items() if value is not None}
        if not body:
            raise ValueError("At least one update field is required")
        return await self._request("PATCH", _note_path(note_id), json_body=body)

    async def delete(self, note_id: str) -> dict[str, Any]:
        return await self._request("DELETE", _note_path(note_id))

    async def restore(self, note_id: str) -> dict[str, Any]:
        return await self._request("POST", f"{_note_path(note_id)}/restore")

    async def health(self) -> dict[str, Any]:
        result = await self._request(
            "GET",
            "/api/pkb/health",
            authenticated=False,
        )
        if result.get("ok") is True:
            result.setdefault("status", "ok")
        return result


def get_pkb_client() -> PkbClient:
    global _pkb_client
    if _pkb_client is None:
        _pkb_client = PkbClient()
    return _pkb_client


async def close_pkb_client() -> None:
    global _pkb_client
    client = _pkb_client
    _pkb_client = None
    if client is not None:
        await client.aclose()
