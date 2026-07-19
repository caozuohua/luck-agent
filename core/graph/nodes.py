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
    tool_list = tools.list() if hasattr(tools, "list") else list(tools)
    task_prompt = await prompt_builder.build_task_prompt_with_experience_search(
        intent_classifier.classify(state.get("goal", "")),
        tool_list,
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
    hitl: bool = False,
) -> dict[str, Any]:
    """Supervise step: verify the step result and decide pass/retry/block/fail.

    A `block` decision pauses the graph via LangGraph interrupt() for
    human-in-the-loop approval (only when `hitl=True`, e.g. an API/cron
    caller wired to a /approve endpoint). When HITL is not available the
    graph must never hang waiting for an approval that will never come, so
    `block` degrades to a clear FAIL answer instead of an empty reply.
    """
    parsed = state.get("last_parsed") or {}

    # Respect a terminal decision already set upstream (executor_node sets
    # DONE/FAIL when there was no tool_call to run).
    upstream = state.get("decision")
    if upstream in (DECISION_DONE, DECISION_FAIL):
        return {
            **state,
            "decision": upstream,
            "final_answer": state.get("final_answer") or parsed.get("message", ""),
        }

    # A CHAT/DONE reply IS the answer — terminal regardless of the flag.
    if parsed.get("intent") in ("CHAT", "DONE"):
        return {**state, "decision": DECISION_DONE, "final_answer": parsed.get("message", "")}

    # Parse failed entirely (no usable model output) -> terminal failure
    # with a clear message rather than an empty reply.
    if parsed is None or not parsed.get("intent"):
        return {
            **state,
            "decision": DECISION_FAIL,
            "final_answer": "（我没能理解或生成有效的执行指令，请换一种说法重试。）",
        }

    if state.get("last_tool_result") is None:
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
        if hitl:
            # LangGraph HITL: pause for operator approval.
            approval = interrupt({"question": dec.reason, "decision": "block"})
            if not (isinstance(approval, dict) and approval.get("approve")):
                return {**state, "decision": DECISION_FAIL, "final_answer": "Blocked by operator."}
            decision = DECISION_PASS
        else:
            # No human-in-the-loop available: degrade to a clear failure so
            # the user gets a useful message instead of an empty reply.
            reason = dec.reason or "step blocked"
            return {
                **state,
                "decision": DECISION_FAIL,
                "final_answer": f"（执行被阻断：{reason}。请调整请求或提供更多上下文后重试。）",
            }
    return {**state, "decision": decision}


async def responder_node(state: AgentState) -> dict[str, Any]:
    """Format the final answer for the user."""
    if state.get("final_answer"):
        return state
    # No answer was set by a node: synthesize a clean, non-empty message so
    # the user never sees a blank "（未生成回复）". Prefer the last model
    # message, then a short note about the last tool outcome.
    parsed = state.get("last_parsed") or {}
    if parsed.get("message"):
        return {**state, "final_answer": parsed["message"]}
    scratchpad = state.get("scratchpad", [])
    last_obs = ""
    for entry in reversed(scratchpad):
        if entry.get("role") == "observation":
            last_obs = entry.get("content", "")
            break
    if last_obs:
        try:
            obs = json.loads(last_obs)
            err = obs.get("error") or obs.get("data", {}).get("output", "")
        except Exception:
            err = last_obs
        err = str(err)[:200]
        msg = f"（任务未能完成：{err}）" if err else "（任务未能完成。）"
        return {**state, "final_answer": msg}
    return {**state, "final_answer": "（任务未能完成，请换一种说法或提供更多上下文。）"}
