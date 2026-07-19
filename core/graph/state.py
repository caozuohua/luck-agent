from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """LangGraph state carried across ReAct loop nodes.

    total=False so a minimal seed dict (goal/user_id) is a valid state.
    `messages` is the LLM message history; `scratchpad` is the
    thought/observation log surfaced back to the planner each step.
    """

    goal: str
    user_id: str
    messages: list[dict[str, Any]]
    scratchpad: list[dict[str, str]]
    step_count: int
    last_tool_result: dict[str, Any] | None
    last_parsed: dict[str, Any] | None
    decision: str | None  # pass | retry | block | fail | done
    final_answer: str
    is_goal_complete: bool
    max_steps: int
