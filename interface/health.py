from __future__ import annotations

import asyncio
import html
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from interface.lark_oauth import LarkOAuthManager
from core.log import get_logger
from memory.db import Database
from memory.goal_store import GoalStatus, GoalStore

log = get_logger("interface.health")


class HealthService:
    def __init__(
        self,
        *,
        db: Database,
        goal_store: GoalStore,
        curator_last_run_at: float | None = None,
        curator: Any | None = None,
        llm: Any | None = None,
        lark_oauth: LarkOAuthManager | None = None,
        lark_oauth_callback_port: int = 8090,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.db = db
        self.goal_store = goal_store
        self.curator_last_run_at = curator_last_run_at
        self.curator = curator
        self.llm = llm
        self.lark_oauth = lark_oauth
        self.lark_oauth_callback_port = int(lark_oauth_callback_port)
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._oauth_server: asyncio.AbstractServer | None = None

    async def collect_status(self) -> dict[str, Any]:
        sqlite_connected = await self._sqlite_connected()
        goal_stats = await self._goal_stats()
        return {
            "process": {
                "status": "ok",
                "timestamp": time.time(),
            },
            "goals": goal_stats,
            "sqlite": {
                "connected": sqlite_connected,
                "path": str(self.db.path),
            },
            "curator": {
                "last_run_at": self._curator_last_run_at(),
            },
            "llm": self._llm_status(),
        }

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
        )
        if self.lark_oauth is not None:
            self._oauth_server = await asyncio.start_server(
                self._handle_oauth_client,
                "127.0.0.1",
                self.lark_oauth_callback_port,
            )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if self._oauth_server is not None:
            self._oauth_server.close()
            await self._oauth_server.wait_closed()
            self._oauth_server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_line = await reader.readline()
        request_target = ""
        try:
            parts = request_line.decode("latin-1").split()
            request_target = parts[1] if len(parts) >= 2 else ""
        except UnicodeDecodeError:
            request_target = ""
        if urlsplit(request_target).path == "/oauth/lark/callback":
            await self._handle_lark_oauth_callback(request_target, writer)
            return
        await reader.read(4096)
        payload = await self.collect_status()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._send_response(writer, 200, "application/json; charset=utf-8", body)

    async def _handle_oauth_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_line = await reader.readline()
        request_target = ""
        try:
            parts = request_line.decode("latin-1").split()
            request_target = parts[1] if len(parts) >= 2 else ""
        except UnicodeDecodeError:
            request_target = ""
        await reader.read(4096)
        await self._handle_lark_oauth_callback(request_target, writer)

    async def _handle_lark_oauth_callback(
        self,
        request_target: str,
        writer: asyncio.StreamWriter,
    ) -> None:
        if self.lark_oauth is None or not self.lark_oauth.configured:
            body = _oauth_html("授权回调未配置", ok=False)
            await self._send_response(writer, 503, "text/html; charset=utf-8", body)
            return
        query = parse_qs(urlsplit(request_target).query, keep_blank_values=True)
        code = _first_query_value(query, "code")
        state = _first_query_value(query, "state")
        error = _first_query_value(query, "error")
        started_at = time.monotonic()
        log.info(
            "lark_oauth_callback_received",
            has_code=bool(code),
            has_state=bool(state),
            has_error=bool(error),
        )
        result = await self.lark_oauth.handle_callback(
            code=code,
            state=state,
            error=error,
        )
        log.info(
            "lark_oauth_callback_completed",
            ok=result.ok,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        status = 200 if result.ok else 400
        await self._send_response(
            writer,
            status,
            "text/html; charset=utf-8",
            _oauth_html(result.detail, ok=result.ok),
        )

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        reason = "OK" if status == 200 else "Service Unavailable" if status == 503 else "Bad Request"
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(headers + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _sqlite_connected(self) -> bool:
        try:
            row = await self.db.fetchone("SELECT 1 AS ok")
            return bool(row and row["ok"] == 1)
        except Exception:
            return False

    def _curator_last_run_at(self) -> float | None:
        if self.curator is not None:
            return getattr(self.curator, "last_run_at", None)
        return self.curator_last_run_at

    def _llm_status(self) -> dict[str, Any]:
        if self.llm is None:
            return {"configured": False, "active_provider": "", "providers": []}
        status_fn = getattr(self.llm, "status", None)
        if not callable(status_fn):
            return {
                "configured": True,
                "active_provider": "unknown",
                "providers": [],
            }
        try:
            status = status_fn()
        except Exception:
            return {
                "configured": True,
                "active_provider": "unknown",
                "providers": [],
                "status_error": True,
            }
        if not isinstance(status, dict):
            return {
                "configured": True,
                "active_provider": "unknown",
                "providers": [],
            }
        return {
            "configured": True,
            "active_provider": str(status.get("active_provider") or ""),
            "providers": status.get("providers", []),
        }

    async def _goal_stats(self) -> dict[str, Any]:
        rows = await self.db.fetchall(
            """
            SELECT status, COUNT(*) AS count
            FROM goals
            GROUP BY status
            """
        )
        counts = {row["status"]: int(row["count"]) for row in rows}
        done = counts.get(GoalStatus.DONE.value, 0)
        failed = counts.get(GoalStatus.FAILED.value, 0)
        total = done + failed
        return {
            "recent_total": total,
            "done": done,
            "failed": failed,
            "success_rate": (done / total) if total else 0.0,
        }


def _first_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0] if values else "")


def _oauth_html(detail: str, *, ok: bool) -> bytes:
    title = "Luck Agent 授权完成" if ok else "Luck Agent 授权未完成"
    message = html.escape(str(detail or ""), quote=True)
    markup = (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title><body>"
        f"<h2>{html.escape(title)}</h2><p>{message}</p>"
        "<p>可以关闭此页面，返回 Lark 继续操作。</p></body></html>"
    )
    return markup.encode("utf-8")
