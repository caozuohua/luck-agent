from __future__ import annotations

import asyncio
import json
from typing import Any

from core.graph.contract import (
    DECISION_BLOCK,
    DECISION_DONE,
    DECISION_FAIL,
    DECISION_PASS,
)
from core.output_parser import OutputParser, ParseError
from core.supervisor import Supervisor
from core.tool_executor import ToolExecutor
from langgraph.types import interrupt


async def _safe_parse(parser: OutputParser, raw: str) -> dict[str, Any] | None:
    try:
        parsed = parser.parse(raw)
    except ParseError as exc:
        try:
            parsed = await parser.repair_and_retry(raw, exc)
        except Exception:
            return None
    # Normalize ParsedOutput -> plain dict for state carry.
    return {
        "intent": getattr(parsed, "intent", None).value
        if getattr(parsed, "intent", None) is not None
        else None,
        "plan": getattr(parsed, "plan", ""),
        "message": getattr(parsed, "message", ""),
        "tool_call": (
            {
                "name": getattr(parsed.tool_call, "name", ""),
                "args": getattr(parsed.tool_call, "args", {}) or {},
            }
            if getattr(parsed, "tool_call", None) is not None
            else None
        ),
        "is_goal_complete": False,
    }


async def planner_node(
    state: AgentState,
    *,
    llm,
    tools: list[Any],
    history: str,
    prompt_builder,
    parser: OutputParser,
    intent_classifier,
    router,
) -> dict[str, Any]:
    """Think step: build the ReAct prompt (with prior observations) and call the LLM."""
    system_prompt = prompt_builder.build_system_prompt()
    task_prompt = await prompt_builder.build_task_prompt_with_experience_search(
        intent_classifier.classify(state.get("goal", "")),
        tools,
        history,
        user_input=state.get("goal", ""),
        experience_patterns=[],
    )
    # Append the running observation log so the model sees prior steps.
    if state.get("scratchpad"):
        log = "\n".join(f"{t['role']}: {t['content']}" for t in state["scratchpad"])
        task_prompt = f"{task_prompt}\n\n# Prior steps\n{log}"

    raw = await llm.generate(system_prompt, task_prompt)
    parsed = await _safe_parse(parser, raw)
    messages = list(state.get("messages", []))
    messages.append({"role": "assistant", "content": raw})
    scratchpad = list(state.get("scratchpad", []))
    scratchpad.append({"role": "thought", "content": raw})

    return {
        **state,
        "messages": messages,
        "scratchpad": scratchpad,
        "last_parsed": parsed,
        "step_count": state.get("step_count", 0) + 1,
        "decision": None,
    }


async def executor_node(state: AgentState, *, tools: ToolExecutor) -> dict[str, Any]:
    """Act step: run the tool named in last_parsed.tool_call and capture the Observation."""
    parsed = state.get("last_parsed") or {}
    tc = parsed.get("tool_call")
    if not tc or not tc.get("name"):
        intent = (parsed or {}).get("intent")
        return {
            **state,
            "decision": DECISION_DONE if intent in ("CHAT", "DONE") else DECISION_FAIL,
        }
    result = await tools.execute(
        str(tc["name"]), dict(tc.get("args", {})), user_id=state.get("user_id", "default")
    )
    rd = result.to_dict()
    scratchpad = list(state.get("scratchpad", []))
    scratchpad.append({"role": "observation", "content": json.dumps(rd, ensure_ascii=False)})
    return {
        **state,
        "last_tool_result": rd,
        "scratchpad": scratchpad,
    }


async def supervisor_node(
    state: AgentState,
    *,
    supervisor: Supervisor,
    goal: dict[str, Any],
    max_retry: int,
) -> dict[str, Any]:
    """Supervise step: verify the step result and decide pass/retry/block/fail.

    A `block` decision pauses the graph via LangGraph interrupt() for
    human-in-the-loop approval; resume continues without re-running steps.
    """
    parsed = state.get("last_parsed") or {}
    if (parsed.get("intent") in ("CHAT", "DONE")) and parsed.get("is_goal_complete"):
        return {**state, "decision": DECISION_DONE, "final_answer": parsed.get("message", "")}
    if state.get("last_tool_result") is None and parsed.get("intent") not in ("CHAT", "DONE"):
        # No action taken and no tool result -> nothing to supervise yet.
        return {**state, "decision": DECISION_DONE, "final_answer": parsed.get("message", "")}

    # Wrap the tool result into the shape Supervisor expects.
    tr = state.get("last_tool_result") or {}
    wrapped = type("R", (), {})()
    wrapped.ok = tr.get("status") == "ok"
    wrapped.error = tr.get("error")
    wrapped.hint = ""
    wrapped.blocking = False
    wrapped.action = (parsed.get("tool_call") or {}).get("name", "")

    dec = supervisor.review_step_result(
        goal=goal,
        step={},
        result=wrapped,
        retry_count=state.get("step_count", 0),
        max_retry=max_retry,
    )
    decision = dec.decision
    if decision == DECISION_BLOCK:
        # LangGraph HITL: pause for operator approval.
        approval = interrupt({"question": dec.reason, "decision": "block"})
        if not (isinstance(approval, dict) and approval.get("approve")):
            return {**state, "decision": DECISION_FAIL, "final_answer": "Blocked by operator."}
        decision = DECISION_PASS
    return {**state, "decision": decision}


async def responder_node(state: AgentState) -> dict[str, Any]:
    """Format the final answer for the user."""
    if not state.get("final_answer"):
        scratchpad = state.get("scratchpad", [])
        last_text = scratchpad[-1].get("content", "") if scratchpad else ""
        # Prefer a CHAT/DONE message if present in last_parsed.
        parsed = state.get("last_parsed") or {}
        answer = parsed.get("message") or last_text or "（任务未完成）"
        return {**state, "final_answer": answer}
    return state
