from __future__ import annotations

import time

from memory.db import Database


class TargetSelectionStore:
    """Persist the selected VPS target per Lark user and chat."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, user_id: str, chat_id: str = "") -> str | None:
        row = await self.db.fetchone(
            """
            SELECT target_id
            FROM target_selections
            WHERE user_id = ? AND chat_id = ?
            """,
            (str(user_id or "default"), str(chat_id or "")),
        )
        return str(row["target_id"]) if row else None

    async def set(self, user_id: str, chat_id: str, target_id: str) -> None:
        await self.db.execute(
            """
            INSERT INTO target_selections (user_id, chat_id, target_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET
                target_id = excluded.target_id,
                updated_at = excluded.updated_at
            """,
            (
                str(user_id or "default"),
                str(chat_id or ""),
                str(target_id),
                int(time.time()),
            ),
        )
