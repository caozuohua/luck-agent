from __future__ import annotations

import asyncio

import pytest

from memory.goal_store import GoalStatus, GoalStore
from runtime.graph_runtime import GraphRuntime


class FakeGraphExecutor:
    def __init__(self, *, answer: str = "执行完成", decision: str = "done") -> None:
        self.answer = answer
        self.decision = decision
        self.requests = []

    async def execute(self, request, *, hitl: bool = False):
        self.requests.append((request, hitl))
        return {"decision": self.decision, "final_answer": self.answer}


async def _wait_for_status(store: GoalStore, goal_id: str, status: GoalStatus):
    async def wait() -> object:
        while True:
            goal = await store.get(goal_id)
            if goal is not None and goal.status is status:
                return goal
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.asyncio
async def test_graph_runtime_accepts_executes_and_notifies_goal(memory_db) -> None:
    store = GoalStore(memory_db)
    executor = FakeGraphExecutor()
    notifications: list[dict] = []

    async def notify(goal: dict) -> None:
        notifications.append(goal)

    runtime = GraphRuntime(
        goal_store=store,
        graph_executor=executor,
        max_active=2,
        terminal_callback=notify,
    )

    await runtime.start()
    try:
        accepted = await runtime.handle_message(
            user_id="u1",
            chat_id="c1",
            text="检查服务状态",
            message_id="m1",
        )
        goal = await _wait_for_status(store, accepted.goal_id, GoalStatus.DONE)

        assert accepted.handled is True
        assert goal.result == "执行完成"
        assert goal.chat_id == "c1"
        assert len(executor.requests) == 1
        assert notifications[0]["chat_id"] == "c1"
        assert notifications[0]["status"] == "DONE"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_graph_runtime_recovers_non_terminal_goals(memory_db) -> None:
    store = GoalStore(memory_db)
    goal = await store.create("u2", "恢复执行", chat_id="c2")
    await store.update_status(goal.id, GoalStatus.ROUTING)
    await store.update_status(goal.id, GoalStatus.PLANNING)
    await store.update_status(goal.id, GoalStatus.EXECUTING)
    executor = FakeGraphExecutor(answer="恢复完成")
    runtime = GraphRuntime(goal_store=store, graph_executor=executor)

    recovered = await runtime.start()
    try:
        assert recovered == 1
        final_goal = await _wait_for_status(store, goal.id, GoalStatus.DONE)
        assert final_goal.result == "恢复完成"
        assert len(executor.requests) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_graph_runtime_marks_non_done_graph_result_failed(memory_db) -> None:
    store = GoalStore(memory_db)
    runtime = GraphRuntime(
        goal_store=store,
        graph_executor=FakeGraphExecutor(answer="达到步骤上限", decision=""),
    )

    await runtime.start()
    try:
        accepted = await runtime.handle_message(
            user_id="u3",
            chat_id="c3",
            text="执行复杂任务",
        )
        goal = await _wait_for_status(store, accepted.goal_id, GoalStatus.FAILED)
        assert goal.error == "达到步骤上限"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_graph_runtime_reuses_persistent_chat_history(memory_db) -> None:
    store = GoalStore(memory_db)
    executor = FakeGraphExecutor()
    runtime = GraphRuntime(goal_store=store, graph_executor=executor)

    await runtime.start()
    try:
        first = await runtime.handle_message(
            user_id="u4",
            chat_id="c4",
            text="我的项目叫 Luck Agent",
        )
        await _wait_for_status(store, first.goal_id, GoalStatus.DONE)

        second = await runtime.handle_message(
            user_id="u4",
            chat_id="c4",
            text="刚才我的项目叫什么？",
        )
        await _wait_for_status(store, second.goal_id, GoalStatus.DONE)

        assert len(executor.requests) == 2
        assert "我的项目叫 Luck Agent" in executor.requests[1][0].history
        assert "执行完成" in executor.requests[1][0].history
    finally:
        await runtime.stop()
