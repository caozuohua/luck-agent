from __future__ import annotations

import pytest

from core.agent import AgentState, MinimalAgent
from core.router import ToolRouter
from memory.goal_store import GoalStatus, GoalStore
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "general_search"
    description = "Search"
    args_schema = {}
    calls = 0

    async def run(self, **kwargs: object) -> ToolResult:
        self.calls += 1
        return ToolResult.ok(data={"text": kwargs.get("text", "ok")}, tool_name=self.name)


class FailTool(Tool):
    name = "general_search"
    description = "Search"
    args_schema = {}

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult.fail(error="provider failed", tool_name=self.name)


class ActionLLM:
    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return '{"intent":"ACTION","plan":"search","tool_call":{"name":"general_search","args":{"text":"done"}},"fallback":"tell user"}'


class ClarifyLLM:
    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return '{"intent":"CLARIFY","question":"Which thing?","best_guess":"Use default"}'


class RecordingGoalStore(GoalStore):
    def __init__(self, db) -> None:
        super().__init__(db)
        self.transitions: list[GoalStatus] = []

    def schedule_status_update(self, goal_id: str, status: GoalStatus, **kwargs):
        self.transitions.append(status)
        return super().schedule_status_update(goal_id, status, **kwargs)


@pytest.mark.asyncio
async def test_complete_action_goal_writes_each_state(memory_db) -> None:
    goal_store = RecordingGoalStore(memory_db)
    registry = ToolRegistry([EchoTool()])
    agent = MinimalAgent(
        llm_client=ActionLLM(),
        tool_registry=registry,
        router=ToolRouter(registry),
        goal_store=goal_store,
    )

    response = await agent.run_turn("search docs", user_id="u1")
    await agent.drain_background_tasks()

    assert "done" in response
    assert agent.state is AgentState.DONE
    assert goal_store.transitions == [
        GoalStatus.ROUTING,
        GoalStatus.PLANNING,
        GoalStatus.EXECUTING,
        GoalStatus.AWAITING_RESULT,
        GoalStatus.EVALUATING,
        GoalStatus.DONE,
    ]


@pytest.mark.asyncio
async def test_tool_failure_moves_goal_to_failed_and_records_fallback(memory_db) -> None:
    goal_store = RecordingGoalStore(memory_db)
    registry = ToolRegistry([FailTool()])
    agent = MinimalAgent(
        llm_client=ActionLLM(),
        tool_registry=registry,
        router=ToolRouter(registry),
        goal_store=goal_store,
    )

    response = await agent.run_turn("search docs", user_id="u1")
    await agent.drain_background_tasks()
    recent = await goal_store.get_recent("u1", limit=1)

    assert "provider failed" in response
    assert "tell user" in response
    assert agent.state is AgentState.FAILED
    assert GoalStatus.FAILED in goal_store.transitions
    assert "tell user" in recent[0].tool_calls


@pytest.mark.asyncio
async def test_clarify_intent_does_not_call_tool(memory_db) -> None:
    goal_store = RecordingGoalStore(memory_db)
    tool = EchoTool()
    registry = ToolRegistry([tool])
    agent = MinimalAgent(
        llm_client=ClarifyLLM(),
        tool_registry=registry,
        router=ToolRouter(registry),
        goal_store=goal_store,
    )

    response = await agent.run_turn("这个怎么弄", user_id="u1")
    await agent.drain_background_tasks()

    assert "Which thing?" in response
    assert tool.calls == 0
    assert GoalStatus.EXECUTING not in goal_store.transitions


@pytest.mark.asyncio
async def test_restart_recovery_returns_executing_goal(memory_db) -> None:
    store = GoalStore(memory_db)
    goal = await store.create("u1", "search docs")
    await store.update_status(goal.id, GoalStatus.ROUTING)
    await store.update_status(goal.id, GoalStatus.PLANNING)
    await store.update_status(goal.id, GoalStatus.EXECUTING)

    recovered_store = GoalStore(memory_db)
    in_progress = await recovered_store.get_in_progress("u1")

    assert [goal.status for goal in in_progress] == [GoalStatus.EXECUTING]
