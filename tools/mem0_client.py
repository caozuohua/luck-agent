from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


class Mem0ClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mem0Health:
    ok: bool
    latency_ms: int
    detail: str = ""


@dataclass(frozen=True)
class Mem0SmokeResult:
    ok: bool
    marker: str
    added: int = 0
    found: int = 0
    deleted: int = 0
    cleanup_confirmed: bool = False
    detail: str = ""


class Mem0Client:
    """Small authenticated client for the self-hosted Mem0 API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        user_id: str = "personal",
        agent_id: str = "luck-agent",
        scope_mode: str = "configured",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.user_id = user_id.strip() or "personal"
        self.agent_id = agent_id.strip() or "luck-agent"
        normalized_scope_mode = scope_mode.strip().lower()
        self.scope_mode = (
            normalized_scope_mode
            if normalized_scope_mode in {"configured", "lark_user"}
            else "configured"
        )
        self.timeout_seconds = timeout_seconds
        self._visible_memory_ids: dict[str, set[str]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def effective_user_id(self, actor_id: str = "") -> str:
        actor = str(actor_id or "").strip()
        if self.scope_mode == "lark_user" and actor and actor.lower() != "default":
            return actor
        return self.user_id

    def scope_label(self, actor_id: str = "") -> str:
        return (
            f"mode={self.scope_mode} · user={self.effective_user_id(actor_id)}"
            f" · agent={self.agent_id}"
        )

    async def health(self) -> Mem0Health:
        started = time.perf_counter()
        try:
            response = await self._request("GET", "/openapi.json", authenticated=False)
            response.raise_for_status()
            return Mem0Health(
                ok=True,
                latency_ms=_elapsed_ms(started),
                detail="openapi reachable",
            )
        except (httpx.HTTPError, Mem0ClientError) as exc:
            return Mem0Health(
                ok=False,
                latency_ms=_elapsed_ms(started),
                detail=str(exc),
            )

    async def add(
        self,
        text: str,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
        actor_id: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": text}],
            "user_id": self.effective_user_id(actor_id),
            "agent_id": self.agent_id,
            "infer": infer,
        }
        if run_id:
            payload["run_id"] = run_id
        if metadata:
            payload["metadata"] = metadata
        data = await self._json_request("POST", "/memories", payload)
        self._remember_visible_ids(data, actor_id)
        return data

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        actor_id: str = "",
    ) -> list[dict[str, Any]]:
        payload = {
            "query": query,
            "filters": {
                "user_id": self.effective_user_id(actor_id),
                "agent_id": self.agent_id,
            },
            "top_k": max(1, min(top_k, 20)),
        }
        data = await self._json_request("POST", "/search", payload)
        results = _results(data)
        self._remember_visible_ids(results, actor_id)
        return results

    async def list_memories(
        self,
        *,
        limit: int = 10,
        actor_id: str = "",
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            "/memories",
            params={
                "user_id": self.effective_user_id(actor_id),
                "agent_id": self.agent_id,
                "page": 1,
                "page_size": max(1, min(limit, 20)),
            },
        )
        response.raise_for_status()
        results = _results(response.json())
        self._remember_visible_ids(results, actor_id)
        return results

    async def delete(self, memory_id: str, *, actor_id: str = "") -> None:
        if self.scope_mode == "lark_user":
            visible = self._visible_memory_ids.get(self.effective_user_id(actor_id), set())
            if memory_id not in visible:
                raise Mem0ClientError(
                    "记忆不在当前用户 scope；请先执行 /mem0 list 或 /mem0 search"
                )
        response = await self._request("DELETE", f"/memories/{memory_id}")
        response.raise_for_status()
        self._visible_memory_ids.get(self.effective_user_id(actor_id), set()).discard(memory_id)

    async def smoke(self, *, actor_id: str = "") -> Mem0SmokeResult:
        marker = f"luck-agent-smoke-{uuid.uuid4().hex[:12]}"
        if not self.api_key:
            return Mem0SmokeResult(
                ok=False,
                marker=marker,
                detail="MEM0_API_KEY 未配置",
            )

        try:
            added_payload = await self.add(
                f"Temporary connectivity marker: {marker}",
                run_id=marker,
                metadata={"source": "luck-agent-smoke", "ephemeral": True},
                infer=False,
                actor_id=actor_id,
            )
            added_items = _results(added_payload)
            added_ids = [str(item["id"]) for item in added_items if item.get("id")]
            found = await self.search(marker, top_k=5, actor_id=actor_id)
            deleted = 0
            for memory_id in added_ids:
                await self.delete(memory_id, actor_id=actor_id)
                deleted += 1
            cleanup_confirmed = bool(added_ids) and deleted == len(added_ids)
            return Mem0SmokeResult(
                ok=bool(added_items) and any(marker in _item_text(item) for item in found),
                marker=marker,
                added=len(added_items),
                found=len(found),
                deleted=deleted,
                cleanup_confirmed=cleanup_confirmed,
                detail="" if cleanup_confirmed else "未能确认临时记录已清理",
            )
        except (httpx.HTTPError, Mem0ClientError, KeyError, TypeError, ValueError) as exc:
            return Mem0SmokeResult(ok=False, marker=marker, detail=str(exc))

    def _remember_visible_ids(self, payload: Any, actor_id: str) -> None:
        if self.scope_mode != "lark_user":
            return
        scope_user = self.effective_user_id(actor_id)
        visible = self._visible_memory_ids.setdefault(scope_user, set())
        for item in _results(payload):
            memory_id = item.get("id")
            if memory_id:
                visible.add(str(memory_id))

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(method, path, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise Mem0ClientError(f"Mem0 返回格式异常: {type(data).__name__}")
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        if not self.base_url:
            raise Mem0ClientError("MEM0_BASE_URL 未配置")
        headers = {"X-API-Key": self.api_key} if authenticated and self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            return await client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                json=json,
            )


def _results(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("results", "memories", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _item_text(item: dict[str, Any]) -> str:
    for key in ("memory", "text", "content"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    return ""


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
