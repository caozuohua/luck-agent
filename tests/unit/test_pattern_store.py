from __future__ import annotations

import pytest

from memory.pattern_store import PatternStore


@pytest.mark.asyncio
async def test_write_pattern_then_fts_search(memory_db) -> None:
    store = PatternStore(memory_db)
    await store.write_pattern(
        pattern_type="success",
        trigger="search alpha docs",
        tool_name="general_search",
        args_schema={"q": "alpha"},
        outcome="found alpha result",
    )

    results = await store.search_patterns("alpha", limit=3)

    assert len(results) == 1
    assert results[0]["tool_name"] == "general_search"


@pytest.mark.asyncio
async def test_search_patterns_returns_relevant_top_three(memory_db) -> None:
    store = PatternStore(memory_db)
    for index in range(5):
        await store.write_pattern(
            pattern_type="success",
            trigger=f"alpha task {index}",
            tool_name=f"tool_{index}",
            outcome=f"alpha outcome {index}",
        )

    results = await store.search_patterns("alpha", limit=3)

    assert len(results) == 3
    assert all("alpha" in row["trigger"] for row in results)


@pytest.mark.asyncio
async def test_search_patterns_without_match_returns_empty_list(memory_db) -> None:
    store = PatternStore(memory_db)
    await store.write_pattern(
        pattern_type="success",
        trigger="alpha task",
        tool_name="tool",
        outcome="alpha outcome",
    )

    assert await store.search_patterns("missing", limit=3) == []
