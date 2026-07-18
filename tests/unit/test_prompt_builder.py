from __future__ import annotations

from pathlib import Path

import pytest

from core.output_parser import IntentType
from core.prompt_builder import PromptBuilder
from tools.base import Tool, ToolResult


class DummyTool(Tool):
    name = "dummy"
    description = "Dummy tool"
    args_schema = {"type": "object"}

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult.ok(tool_name=self.name)


class MockPatternStore:
    async def search_patterns(self, query: str, limit: int = 3):
        return [
            {
                "trigger": "search alpha",
                "outcome": "use dummy carefully",
                "tool_name": "dummy",
            }
        ]


def _builder(tmp_path: Path, **kwargs) -> PromptBuilder:
    soul = tmp_path / "SOUL.md"
    memory = tmp_path / "MEMORY.md"
    soul.write_text("SOUL CONTENT", encoding="utf-8")
    memory.write_text("MEMORY CONTENT", encoding="utf-8")
    return PromptBuilder(soul_path=soul, memory_path=memory, **kwargs)


def test_layer_1_contains_soul_content(tmp_path: Path) -> None:
    prompt = _builder(tmp_path).build_system_prompt()

    assert "SOUL CONTENT" in prompt
    assert "MEMORY CONTENT" in prompt


def test_layer_2_contains_tool_docstring(tmp_path: Path) -> None:
    prompt = _builder(tmp_path).build_task_prompt(
        IntentType.ACTION,
        [DummyTool()],
        "short history",
        [],
        user_input="run dummy",
    )

    assert "Tool: dummy" in prompt
    assert "Dummy tool" in prompt


@pytest.mark.asyncio
async def test_layer_3_experience_is_injected_from_pattern_store(tmp_path: Path) -> None:
    prompt = await _builder(tmp_path, pattern_store=MockPatternStore()).build_task_prompt_with_experience_search(
        IntentType.ACTION,
        [DummyTool()],
        "",
        user_input="search alpha",
    )

    assert "use dummy carefully" in prompt


def test_history_budget_truncates_history_not_system_prompt(tmp_path: Path) -> None:
    builder = _builder(tmp_path, history_token_budget=4)
    system_prompt = builder.build_system_prompt()
    task_prompt = builder.build_task_prompt(
        IntentType.CHAT,
        [],
        "A" * 200,
        [],
        user_input="hello",
    )

    assert "SOUL CONTENT" in system_prompt
    assert "A" * 200 not in task_prompt
    assert "[truncated]" in task_prompt
