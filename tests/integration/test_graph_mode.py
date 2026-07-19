"""Graph-mode (LangGraph ReAct) integration tests for MinimalAgent.

These exercise the default execution_mode="graph" path end to end:
multi-step Think->Act->Observe->Supervise, tool execution, and
termination via a CHAT/DONE reply.
"""
from __future__ import annotations

import json

import pytest

from core.agent import MinimalAgent
from core.router import ToolRouter
from memory.goal_store import GoalStore
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


class CountingTool(Tool):
    name = "general_search"
    description = "Search"
    args_schema: dict = {}

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **kwargs: object) -> ToolResult:
        self.calls += 1
        return ToolResult.ok(data={"text": f"result-{self.calls}"}, tool_name=self.name)


class ScriptedLLM:
    """Returns a fixed sequence of raw outputs, one per generate() call."""

    model = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        self.calls += 1
        if self._outputs:
            return self._outputs.pop(0)
        # Default: finish.
        return json.dumps({"intent": "CHAT", "message": "fallback done"})

    async def repair(self, raw: str, error: Exception, attempt: int) -> str:
        return json.dumps({"intent": "CHAT", "message": "repaired"})


def _action(tool: str, args: dict) -> str:
    return json.dumps(
        {"intent": "ACTION", "plan": "step", "fallback": "", "tool_call": {"name": tool, "args": args}}
    )


@pytest.mark.asyncio
async def test_graph_multi_step_runs_tool_then_finishes(memory_db, tmp_path) -> None:
    tool = CountingTool()
    registry = ToolRegistry([tool])
    llm = ScriptedLLM(
        [
            _action("general_search", {"text": "q1"}),  # step 1: act
            json.dumps({"intent": "CHAT", "message": "最终答案：完成"}),  # step 2: finish
        ]
    )
    agent = MinimalAgent(
        llm_client=llm,
        tool_registry=registry,
        router=ToolRouter(registry),
        goal_store=GoalStore(memory_db),
        execution_mode="graph",
        max_steps=6,
        graph_db_path=str(tmp_path / "g.db"),
    )

    response = await agent.run_turn("查一下然后告诉我", user_id="u1")
    await agent.drain_background_tasks()

    assert tool.calls >= 1  # the loop actually executed a tool
    assert "最终答案" in response  # the loop terminated on the CHAT reply
    assert llm.calls >= 2  # multi-step: at least act + finish


@pytest.mark.asyncio
async def test_graph_keeps_multi_turn_history(memory_db, tmp_path) -> None:
    registry = ToolRegistry([CountingTool()])
    llm = ScriptedLLM(
        [
            json.dumps({"intent": "CHAT", "message": "你好，我在。"}),
            json.dumps({"intent": "CHAT", "message": "你刚问的是打招呼。"}),
        ]
    )
    agent = MinimalAgent(
        llm_client=llm,
        tool_registry=registry,
        router=ToolRouter(registry),
        goal_store=GoalStore(memory_db),
        execution_mode="graph",
        graph_db_path=str(tmp_path / "g2.db"),
    )

    await agent.run_turn("你好", user_id="u1")
    await agent.run_turn("我刚才说了什么", user_id="u1")

    # Second turn's prompt should include the first turn (history injected).
    assert len(agent.conversation_history) == 4
    assert agent.conversation_history[0]["content"] == "你好"
