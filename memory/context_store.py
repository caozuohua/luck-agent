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
        summary: str,
        turn_range: dict[str, int] | None = None,
    ) -> str:
        summary_id = uuid.uuid4().hex
        await self.db.execute(
            """
            INSERT INTO context_summaries (id, user_id, summary, turn_range, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                summary_id,
                user_id,
                summary,
                json.dumps(turn_range or {}, ensure_ascii=False, sort_keys=True),
                int(time.time()),
            ),
        )
        return summary_id

    async def get_latest_summary(self, user_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT id, user_id, summary, turn_range, created_at
            FROM context_summaries
            WHERE user_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (user_id,),
        )
        if row is None:
            return None
        result = dict(row)
        try:
            result["turn_range"] = json.loads(result.get("turn_range") or "{}")
        except json.JSONDecodeError:
            result["turn_range"] = {}
        return result
