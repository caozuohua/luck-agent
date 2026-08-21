from __future__ import annotations

import asyncio

from core.graph.executor import GraphExecutionRequest, GraphGoalExecutor


def test_graph_executor_uses_goal_scoped_thread(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_graph(state, **kwargs):
        captured["state"] = state
        captured["kwargs"] = kwargs
        return {**state, "final_answer": "ok"}

    monkeypatch.setattr("core.graph.executor.run_graph", fake_run_graph)
    executor = GraphGoalExecutor(
        llm_client=object(),
        tool_registry=object(),
        tool_executor=object(),
        supervisor=object(),
        prompt_builder=object(),
        output_parser=object(),
        intent_classifier=object(),
        router=object(),
        graph_db_path="/tmp/graph.db",
        max_steps=7,
        max_retry=1,
    )

    result = asyncio.run(
        executor.execute(
            GraphExecutionRequest(
                goal_id="goal-1",
                user_id="user-1",
                text="检查状态",
                approval_token="token-1",
                history="previous",
            )
        )
    )

    assert result["final_answer"] == "ok"
    state = captured["state"]
    kwargs = captured["kwargs"]
    assert state["goal"] == "检查状态"
    assert state["approval_token"] == "token-1"
    assert kwargs["config"]["configurable"]["thread_id"] == "user-1:goal-1"
    assert kwargs["db_path"] == "/tmp/graph.db"
    assert kwargs["max_steps"] == 7
    assert kwargs["max_retry"] == 1
    assert kwargs["history"] == "previous"
