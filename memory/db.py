from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import aiosqlite


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.commit()
        return self._conn

    async def initialize(self) -> None:
        conn = await self.connect()
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS goals (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                status      TEXT NOT NULL,
                intent_type TEXT,
                raw_input   TEXT,
                plan        TEXT,
                tool_calls  TEXT,
                result      TEXT,
                error       TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_goals_user_status
                ON goals(user_id, status);

            CREATE TABLE IF NOT EXISTS patterns (
                id           TEXT PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                trigger      TEXT NOT NULL,
                tool_name    TEXT,
                args_schema  TEXT,
                outcome      TEXT,
                user_id      TEXT,
                created_at   INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS patterns_fts USING fts5(
                trigger, outcome, tool_name,
                content=patterns, content_rowid=rowid
            );

            CREATE TABLE IF NOT EXISTS context_summaries (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                summary     TEXT NOT NULL,
                turn_range  TEXT,
                created_at  INTEGER NOT NULL
            );
            """
        )
        await conn.commit()

    async def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        conn = await self.connect()
        await conn.execute(sql, parameters)
        await conn.commit()

    async def fetchone(self, sql: str, parameters: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        conn = await self.connect()
        cursor = await conn.execute(sql, parameters)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def fetchall(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        conn = await self.connect()
        cursor = await conn.execute(sql, parameters)
        try:
            return await cursor.fetchall()
        finally:
            await cursor.close()

    async def insert_pattern(
        self,
        *,
        pattern_id: str,
        pattern_type: str,
        trigger: str,
        tool_name: str,
        args_schema: str,
        outcome: str,
        user_id: str = "",
    ) -> None:
        now = int(time.time())
        conn = await self.connect()
        await conn.execute(
            """
            INSERT INTO patterns (
                id, pattern_type, trigger, tool_name, args_schema, outcome, user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pattern_id, pattern_type, trigger, tool_name, args_schema, outcome, user_id, now),
        )
        await conn.execute(
            "INSERT INTO patterns_fts(rowid, trigger, outcome, tool_name) "
            "SELECT rowid, trigger, outcome, tool_name FROM patterns WHERE id = ?",
            (pattern_id,),
        )
        await conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            # Fold the WAL into the main database and drop the -wal/-shm
            # auxiliary files before closing. On Windows the OS holds these
            # handles briefly, so removing them here avoids teardown rmtree
            # races (PermissionError / WinError 32) in tests.
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await self._conn.execute("PRAGMA journal_mode=DELETE")
                await self._conn.commit()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
            await self._conn.close()
            self._conn = None
