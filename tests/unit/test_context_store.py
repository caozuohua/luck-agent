from __future__ import annotations

import pytest

from memory.context_store import ContextStore


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
async def test_get_latest_summary_returns_none_without_rows(memory_db) -> None:
    assert await ContextStore(memory_db).get_latest_summary("missing") is None
