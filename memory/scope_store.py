from __future__ import annotations

import time

from memory.db import Database


class MemoryScopeStore:
    """Persist the explicitly selected Mem0 project per user and chat."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, user_id: str, chat_id: str = "") -> str | None:
        row = await self.db.fetchone(
            """
            SELECT project_id
            FROM memory_scopes
            WHERE user_id = ? AND chat_id = ?
            """,
            (str(user_id or "default"), str(chat_id or "")),
        )
        return str(row["project_id"]) if row else None

    async def set(self, user_id: str, chat_id: str, project_id: str) -> None:
        await self.db.execute(
            """
            INSERT INTO memory_scopes (user_id, chat_id, project_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                project_id = excluded.project_id,
                updated_at = excluded.updated_at
            """,
            (
                str(user_id or "default"),
                str(chat_id or ""),
                str(project_id),
                int(time.time()),
            ),
        )
