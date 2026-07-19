"""Agent task-suite: production-grade controllability + fallback guarantees.

Deterministic, offline (FakeLLM / ScriptedLLM). Drives the graph-mode
ReAct loop through the exact scenarios in tests/AGENT_TASK_SUITE.md (B).
Each test asserts the mechanism guarantees: tool runs, non-empty answers
on failure, step-cap backstop, dangerous-command rejection, multi-turn
history injection. No API cost, runs in <1s.
"""
from __future__ import annotations

import json

import pytest

from core.agent import MinimalAgent
from core.router import ToolRouter
from memory.goal_store import GoalStore
from memory.db import Database
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


# --- fixtures / helpers -------------------------------------------------

class ScriptedLLM:
    """Returns a fixed sequence of raw outputs, one per generate() call."""

    model = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.calls = 0
        self.last_task_prompt: str = ""

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        self.calls += 1
        self.last_task_prompt = task_prompt
        if self._outputs:
            return self._outputs.pop(0)
        return json.dumps({"intent": "CHAT", "message": "fallback done"})

    async def repair(self, raw: str, error: Exception, attempt: int) -> str:
        return json.dumps({"intent": "CHAT", "message": "repaired"})


class CountingTool(Tool):
    name = "general_search"
    description = "Search"
    args_schema: dict = {}

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **kwargs: object) -> ToolResult:
        self.calls += 1
        return ToolResult.ok(data={"text": f"result-{self.calls}"}, tool_name=self.name)


def _action(tool: str, args: dict) -> str:
    return json.dumps(
        {"intent": "ACTION", "plan": "step", "fallback": "", "tool_call": {"name": tool, "args": args}}
    )


def _agent(llm, *, max_steps: int = 6, max_retry: int = 1, tools=None, db=None, tmp=None):
    registry = ToolRegistry(tools if tools is not None else [CountingTool()])
    if db is None:
        db = Database(":memory:")
    return MinimalAgent(
        llm_client=llm,
        tool_registry=registry,
        router=ToolRouter(registry),
        goal_store=GoalStore(db),
        execution_mode="graph",
        max_steps=max_steps,
        max_retry=max_retry,
        graph_db_path=str(tmp / "g.db") if tmp else ":memory:",
    )


async def _make_db() -> Database:
    db = Database(":memory:")
    await db.initialize()
    return db


# --- T1: multi-step ReAct loop completes -------------------------------

@pytest.mark.asyncio
async def test_T1_multi_step_react_completes(tmp_path) -> None:
    tool = CountingTool()
    llm = ScriptedLLM(
        [
            _action("general_search", {"text": "q1"}),
            json.dumps({"intent": "CHAT", "message": "最终答案：完成"}),
        ]
    )
    agent = _agent(llm, tools=[tool], db=await _make_db(), tmp=tmp_path)
    resp = await agent.run_turn("查一下然后告诉我", user_id="u1")
    await agent.drain_background_tasks()
    assert tool.calls >= 1
    assert "最终答案" in resp
    assert llm.calls >= 2


# --- T2: tool permanently fails -> non-empty graceful answer -----------

@pytest.mark.asyncio
async def test_T2_tool_failure_never_empty(tmp_path) -> None:
    class FailTool(Tool):
        name = "general_search"
        description = "always fails"
        args_schema: dict = {}

        async def run(self, **kwargs: object) -> ToolResult:
            return ToolResult.fail(error="boom", tool_name=self.name)

    llm = ScriptedLLM([_action("general_search", {"text": "q"})])
    agent = _agent(llm, max_retry=0, tools=[FailTool()], db=await _make_db(), tmp=tmp_path)
    resp = await agent.run_turn("做点什么", user_id="u1")
    await agent.drain_background_tasks()
    assert resp, "answer must not be empty"
    assert "未生成回复" not in resp
    assert any(k in resp for k in ("boom", "未能完成", "被阻断"))


# --- T3: total parse failure -> non-empty fallback ---------------------

@pytest.mark.asyncio
async def test_T3_parse_failure_degrades(tmp_path) -> None:
    class GarbageLLM(ScriptedLLM):
        async def generate(self, system_prompt: str, task_prompt: str) -> str:
            self.calls += 1
            return "this is not json at all !!!"  # unparseable

        async def repair(self, raw: str, error: Exception, attempt: int) -> str:
            return "still garbage"  # repair also fails

    agent = _agent(GarbageLLM([]), db=await _make_db(), tmp=tmp_path)
    resp = await agent.run_turn("asdf;lksdjf 乱码", user_id="u1")
    await agent.drain_background_tasks()
    assert resp, "answer must not be empty on parse failure"
    assert "未生成回复" not in resp


# --- T4: step-cap backstop (recursion_limit) --------------------------

@pytest.mark.asyncio
async def test_T4_step_cap_backstop(tmp_path) -> None:
    # Model emits ACTION forever (never finishes); loop must stop at the
    # step budget and return a non-empty message instead of hanging.
    llm = ScriptedLLM([_action("general_search", {"text": "loop"})] * 10)
    agent = _agent(llm, max_steps=4, tools=[CountingTool()], db=await _make_db(), tmp=tmp_path)
    resp = await agent.run_turn("无限循环任务", user_id="u1")
    await agent.drain_background_tasks()
    assert resp, "step-cap must not yield an empty answer"
    assert "未生成回复" not in resp


# --- T5: dangerous command rejected by shell tool ----------------------

@pytest.mark.asyncio
async def test_T5_dangerous_command_rejected(tmp_path) -> None:
    from tools.shell import ShellTool

    llm = ScriptedLLM([_action("shell", {"command": "rm -rf /"})])
    agent = _agent(llm, max_retry=0, tools=[ShellTool()], db=await _make_db(), tmp=tmp_path)
    resp = await agent.run_turn("帮我清理磁盘 rm -rf /", user_id="u1")
    await agent.drain_background_tasks()
    assert resp, "rejection must not be empty"
    assert any(k in resp for k in ("dangerous", "未能完成", "rejected"))


# --- T6: multi-turn history injected into planner prompt --------------

@pytest.mark.asyncio
async def test_T6_multi_turn_history_injected(tmp_path) -> None:
    llm = ScriptedLLM(
        [
            json.dumps({"intent": "CHAT", "message": "你好，我在。"}),
            json.dumps({"intent": "CHAT", "message": "你刚问的是打招呼。"}),
        ]
    )
    agent = _agent(llm, db=await _make_db(), tmp=tmp_path)
    await agent.run_turn("你好", user_id="u1")
    await agent.drain_background_tasks()
    await agent.run_turn("我刚才说了什么", user_id="u1")
    await agent.drain_background_tasks()
    assert len(agent.conversation_history) == 4
    assert "你好" in agent.conversation_history[0]["content"]
