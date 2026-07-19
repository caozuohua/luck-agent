from __future__ import annotations

import asyncio
import json

from core.graph.contract import DECISION_DONE
from core.graph.nodes import executor_node, planner_node, responder_node, supervisor_node
from core.graph.state import AgentState
from core.intent_classifier import IntentClassifier
from core.output_parser import OutputParser
from core.prompt_builder import PromptBuilder
from core.router import ToolRouter
from core.supervisor import Supervisor
from core.tool_executor import ToolExecutor
from llm.fake import FakeLLMClient
from tools.registry import ToolRegistry


def _seed() -> AgentState:
    return AgentState(
        goal="what time is it",
        user_id="u1",
        messages=[],
        scratchpad=[],
        step_count=0,
        last_tool_result=None,
        last_parsed=None,
        decision=None,
        final_answer="",
        is_goal_complete=False,
    )


def _deps(reg: ToolRegistry):
    return dict(
        llm=FakeLLMClient(
            queue=[
                json.dumps(
                    {"intent": "ACTION", "plan": "p", "fallback": "",
                     "tool_call": {"name": "shell", "args": {"command": "date"}}}
                )
            ]
        ),
        tools=reg.list(),
        history="",
        prompt_builder=PromptBuilder(),
        parser=OutputParser(repair_fn=None),
        intent_classifier=IntentClassifier(),
        router=ToolRouter(reg),
    )


def test_planner_appends_message_and_increments_step() -> None:
    reg = ToolRegistry()
    reg.register_builtin_tools()
    s = _seed()
    out = asyncio.run(planner_node(s, **_deps(reg)))
    assert out["step_count"] == 1
    assert out["messages"]
    assert out["last_parsed"] is not None
    assert out["last_parsed"]["intent"] == "ACTION"


def test_executor_runs_tool() -> None:
    reg = ToolRegistry()
    reg.register_builtin_tools()
    s = _seed()
    s["last_parsed"] = {"intent": "ACTION", "tool_call": {"name": "shell", "args": {"command": "echo hello"}}}
    out = asyncio.run(executor_node(s, tools=ToolExecutor(reg)))
    assert out["last_tool_result"] is not None
    assert "hello" in str(out["last_tool_result"].get("data", ""))


def test_supervisor_done_on_complete() -> None:
    s = _seed()
    s["last_parsed"] = {"intent": "CHAT", "is_goal_complete": True, "message": "done!"}
    out = asyncio.run(supervisor_node(s, supervisor=Supervisor(), goal={}, max_retry=2))
    assert out["decision"] == DECISION_DONE
    assert out["final_answer"] == "done!"


def test_responder_fills_final_answer() -> None:
    s = _seed()
    s["scratchpad"] = [{"role": "thought", "content": "answer text"}]
    out = asyncio.run(responder_node(s))
    assert out["final_answer"]
