from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from memory.db import Database
from memory.goal_store import GoalStatus, GoalStore


class HealthService:
    def __init__(
        self,
        *,
        db: Database,
        goal_store: GoalStore,
        curator_last_run_at: float | None = None,
        curator: Any | None = None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.db = db
        self.goal_store = goal_store
        self.curator_last_run_at = curator_last_run_at
        self.curator = curator
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None

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
        }

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.read(4096)
        payload = await self.collect_status()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
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
