from __future__ import annotations

import pytest
import aiosqlite

from memory.context_store import ContextStore
from memory.db import Database


@pytest.mark.asyncio
async def test_save_summary_and_get_latest_summary(memory_db) -> None:
    store = ContextStore(memory_db)
    await store.save_summary(user_id="u1", summary="first", turn_range={"from": 1, "to": 3})
    await store.save_summary(user_id="u1", summary="second", turn_range={"from": 4, "to": 6})

    latest = await store.get_latest_summary("u1")

    assert latest is not None
    assert latest["summary"] == "second"
    assert latest["turn_range"] == {"from": 4, "to": 6}


@pytest.mark.asyncio
async def test_context_summary_isolated_by_chat(memory_db) -> None:
    store = ContextStore(memory_db)
    await store.save_summary(user_id="u1", chat_id="chat-a", summary="A")
    await store.save_summary(user_id="u1", chat_id="chat-b", summary="B")

    chat_a = await store.get_latest_summary("u1", chat_id="chat-a")
    chat_b = await store.get_latest_summary("u1", chat_id="chat-b")

    assert chat_a is not None and chat_a["summary"] == "A"
    assert chat_b is not None and chat_b["summary"] == "B"


@pytest.mark.asyncio
async def test_get_latest_summary_returns_none_without_rows(memory_db) -> None:
    assert await ContextStore(memory_db).get_latest_summary("missing") is None


@pytest.mark.asyncio
async def test_database_migrates_legacy_context_summary_table(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            """
            CREATE TABLE context_summaries (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                turn_range TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        await conn.commit()

    db = Database(path)
    await db.initialize()
    columns = await db.fetchall("PRAGMA table_info(context_summaries)")
    indexes = await db.fetchall("PRAGMA index_list('context_summaries')")

    assert any(row["name"] == "chat_id" for row in columns)
    assert any(row["name"] == "idx_context_summaries_scope" for row in indexes)
    await db.close()
