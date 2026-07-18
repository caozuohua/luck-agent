from __future__ import annotations

from pathlib import Path

import pytest

from memory.curator import Curator
from memory.pattern_store import PatternStore


class CuratorLLM:
    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return "Use general_search for repeated search tasks.\nReport provider failures honestly."


@pytest.mark.asyncio
async def test_curator_generates_memory_for_sixty_patterns_without_duplicates(memory_db, tmp_path: Path) -> None:
    store = PatternStore(memory_db)
    for index in range(60):
        await store.write_pattern(
            pattern_type="success" if index % 2 else "error",
            trigger=f"search task {index}",
            tool_name="general_search",
            outcome="same lesson",
        )
    memory_path = tmp_path / "MEMORY.md"
    curator = Curator(pattern_store=store, llm_client=CuratorLLM(), memory_path=memory_path)

    await curator.run()
    first = memory_path.read_text(encoding="utf-8")
    await curator.run()
    second = memory_path.read_text(encoding="utf-8")

    assert "general_search" in first
    assert len(first) <= 3000
    assert second == first
