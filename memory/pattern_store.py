from __future__ import annotations

import json
import time
import uuid
from typing import Any

from memory.db import Database


class PatternStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def write_pattern(
        self,
        *,
        pattern_type: str,
        trigger: str,
        tool_name: str = "",
        args_schema: str | dict[str, Any] = "",
        outcome: str = "",
        user_id: str = "",
        pattern_id: str | None = None,
    ) -> str:
        if pattern_type not in {"success", "error"}:
            raise ValueError("pattern_type must be 'success' or 'error'")
        pattern_id = pattern_id or uuid.uuid4().hex
        args_text = (
            json.dumps(args_schema, ensure_ascii=False, sort_keys=True)
            if isinstance(args_schema, dict)
            else args_schema
        )
        await self.db.insert_pattern(
            pattern_id=pattern_id,
            pattern_type=pattern_type,
            trigger=trigger,
            tool_name=tool_name,
            args_schema=args_text,
            outcome=outcome,
            user_id=user_id,
        )
        return pattern_id

    async def search_patterns(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        terms = self._fts_query(query)
        if not terms:
            return []
        rows = await self.db.fetchall(
            """
            SELECT p.id, p.pattern_type, p.trigger, p.tool_name, p.args_schema,
                   p.outcome, p.user_id, p.created_at
            FROM patterns_fts f
            JOIN patterns p ON p.rowid = f.rowid
            WHERE patterns_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (terms, limit),
        )
        return [dict(row) for row in rows]

    async def list_patterns(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT id, pattern_type, trigger, tool_name, args_schema, outcome, user_id, created_at
            FROM patterns
            ORDER BY created_at DESC
            """
        )
        return [dict(row) for row in rows]

    async def delete_older_than(self, cutoff_unix: int) -> None:
        rows = await self.db.fetchall("SELECT rowid FROM patterns WHERE created_at < ?", (cutoff_unix,))
        rowids = [row["rowid"] for row in rows]
        if not rowids:
            return
        placeholders = ",".join("?" for _ in rowids)
        await self.db.execute(
            f"DELETE FROM patterns_fts WHERE rowid IN ({placeholders})",
            tuple(rowids),
        )
        await self.db.execute("DELETE FROM patterns WHERE created_at < ?", (cutoff_unix,))

    def _fts_query(self, query: str) -> str:
        # FTS5 MATCH treats / * ( ) " : NEAR ... as syntax. Quote each
        # whitespace-separated token as a safe phrase and OR them, so
        # arbitrary user input can never break the query (a "/" or "(" in
        # the message used to raise "fts5: syntax error near ...") while
        # still matching any of the query terms (recall-preserving).
        tokens = [
            tok.strip()
            for tok in query.replace("'", " ").split()
            if tok.strip()
        ]
        if not tokens:
            return ""
        safe = [f'"{tok.replace(chr(34), chr(34) * 2)}"' for tok in tokens[:8]]
        return " OR ".join(safe)


def pattern_outcome_from_data(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data[:500]
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, sort_keys=True)[:500]
    return str(data)[:500]
