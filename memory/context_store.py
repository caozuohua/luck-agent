from __future__ import annotations

import json
import time
import uuid
from typing import Any

from memory.db import Database


class ContextStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save_summary(
        self,
        *,
        user_id: str,
        chat_id: str = "",
        summary: str,
        turn_range: dict[str, int] | None = None,
    ) -> str:
        summary_id = uuid.uuid4().hex
        await self.db.execute(
            """
            INSERT INTO context_summaries (id, user_id, chat_id, summary, turn_range, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                summary_id,
                user_id,
                chat_id,
                summary,
                json.dumps(turn_range or {}, ensure_ascii=False, sort_keys=True),
                int(time.time()),
            ),
        )
        return summary_id

    async def get_latest_summary(
        self,
        user_id: str,
        chat_id: str = "",
    ) -> dict[str, Any] | None:
        if chat_id:
            where = "user_id = ? AND chat_id = ?"
            parameters = (user_id, chat_id)
        else:
            where = "user_id = ?"
            parameters = (user_id,)
        row = await self.db.fetchone(
            f"""
            SELECT id, user_id, chat_id, summary, turn_range, created_at
            FROM context_summaries
            WHERE {where}
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            parameters,
        )
        if row is None:
            return None
        result = dict(row)
        try:
            result["turn_range"] = json.loads(result.get("turn_range") or "{}")
        except json.JSONDecodeError:
            result["turn_range"] = {}
        return result
