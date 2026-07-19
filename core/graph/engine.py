from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph

from core.graph.contract import DECISION_DONE, DECISION_FAIL, DECISION_PASS, DECISION_RETRY
from core.graph.nodes import executor_node, planner_node, responder_node, supervisor_node
from core.graph.state import AgentState


def build_graph(node_deps: dict[str, Any] | None = None) -> StateGraph:
    """Define the ReAct StateGraph (uncompiled).

    `node_deps` maps node name -> pre-bound callable. When omitted, nodes
    are added bare (for tests that bind deps at run time via partial).
    Nodes: planner -> executor -> supervisor -> (router) -> planner | responder.
    The supervisor node may interrupt() on a `block` decision for HITL.
    """
    g = StateGraph(AgentState)

    def add(name: str, fn):
        g.add_node(name, fn)

    add("planner", partial(planner_node, **(node_deps or {}).get("planner", {})))
    add("executor", partial(executor_node, **(node_deps or {}).get("executor", {})))
    add("supervisor", partial(supervisor_node, **(node_deps or {}).get("supervisor", {})))
    add("responder", responder_node)  # no extra deps

    g.set_entry_point("planner")
    g.add_edge("planner", "executor")
    g.add_edge("executor", "supervisor")

    def route(state: AgentState) -> str:
        decision = state.get("decision")
        if decision in (DECISION_DONE, DECISION_FAIL):
            return "responder"
        # Hard step cap (production resource control): stop looping once the
        # budget is spent, even if the model keeps emitting ACTIONs.
        max_steps = state.get("max_steps") or 12
        if state.get("step_count", 0) >= max_steps:
            return "responder"
        return "planner"  # pass | retry | (block handled inside node)

    g.add_conditional_edges("supervisor", route)
    g.add_edge("responder", END)
    return g


@asynccontextmanager
async def _saver(db_path: str):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        yield saver


def _bind_deps(**deps: Any) -> dict[str, dict[str, Any]]:
    """Split flat deps into per-node keyword bindings.

    Shared deps (llm, supervisor, history, prompt_builder, parser,
    intent_classifier, router) go to planner; tools+executor go to
    executor; supervisor gets supervisor+goal.
    """
    planner_keys = {"llm", "tools", "history", "prompt_builder", "parser", "intent_classifier", "router"}
    return {
        "planner": {k: deps[k] for k in planner_keys if k in deps},
        "executor": {"tools": deps["executor"]} if "executor" in deps else {"tools": deps["tools"]},
        "supervisor": {
            "supervisor": deps["supervisor"],
            "goal": deps.get("goal", {}),
            "max_retry": deps.get("max_retry", 2),
        },
    }


async def run_graph(
    state: AgentState,
    *,
    graph: StateGraph | None = None,
    config: dict[str, Any],
    max_steps: int = 12,
    db_path: str = "graph_state.db",
    **deps: Any,
) -> AgentState:
    """Compile (with SQLite checkpointer + recursion cap) and run to END.

    `deps` are per-run dependencies (llm, tools/executor, supervisor,
    prompt_builder, parser, intent_classifier, router, history, goal).
    On `GraphRecursionError` (step cap hit) returns a graceful answer.
    """
    node_deps = _bind_deps(**deps)
    g = graph or build_graph(node_deps)
    # Carry the step budget in state so the router can enforce a hard cap
    # (recursion_limit is a backstop set comfortably above it).
    state = {**state, "max_steps": max_steps}
    async with _saver(db_path) as saver:
        app = g.compile(checkpointer=saver)
        try:
            result = await app.ainvoke(state, config, recursion_limit=max_steps * 3 + 5)
        except GraphRecursionError:
            result = {
                **state,
                "final_answer": "（任务步骤超出上限，已停止。请拆分任务或简化目标。）",
            }
        return result


async def resume_graph(
    graph: StateGraph,
    thread_id: str,
    approval: dict[str, Any],
    *,
    db_path: str = "graph_state.db",
    max_steps: int = 12,
    **node_deps: Any,
) -> AgentState:
    """Resume a graph paused at an interrupt() (human-in-the-loop block)."""
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread_id}}
    g = build_graph(node_deps) if node_deps else graph
    async with _saver(db_path) as saver:
        app = g.compile(checkpointer=saver)
        return await app.ainvoke(Command(resume=approval), config)
