from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from core.graph.engine import build_graph, run_graph
from core.graph.state import AgentState
from core.intent_classifier import IntentClassifier
from core.output_parser import OutputParser
from core.prompt_builder import PromptBuilder
from core.router import ToolRouter
from core.supervisor import Supervisor
from core.tool_executor import ToolExecutor
from llm.fake import FakeLLMClient
from tools.registry import ToolRegistry


def _graph():
    return build_graph()


def _seed(goal: str = "hi") -> AgentState:
    return AgentState(
        goal=goal,
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
        llm=FakeLLMClient(default_intent=__import__("core.output_parser", fromlist=["IntentType"]).IntentType.CHAT),
        tools=reg,
        supervisor=Supervisor(),
        history="",
        prompt_builder=PromptBuilder(),
        parser=OutputParser(repair_fn=None),
        intent_classifier=IntentClassifier(),
        router=ToolRouter(reg),
        executor=ToolExecutor(reg),
    )


def test_graph_runs_to_completion_offline() -> None:
    reg = ToolRegistry()
    reg.register_builtin_tools()
    out = asyncio.run(
        run_graph(_seed(), config={"configurable": {"thread_id": "t-comp"}}, max_steps=5, **_deps(reg))
    )
    assert isinstance(out["final_answer"], str)


def test_recursion_limit_stops_loop() -> None:
    """A model that always returns ACTION must not loop forever."""
    from core.output_parser import IntentType

    reg = ToolRegistry()
    reg.register_builtin_tools()
    # Always emit ACTION (shell echo) -> would loop forever without cap.
    always_action = FakeLLMClient(
        queue=[json.dumps({"intent": "ACTION", "plan": "p", "fallback": "", "tool_call": {"name": "shell", "args": {"command": "echo x"}}})]
    )
    out = asyncio.run(
        run_graph(
            _seed("loop"),
            config={"configurable": {"thread_id": "t-rec"}},
            max_steps=3,
            llm=always_action,
            tools=reg,
            supervisor=Supervisor(),
            history="",
            prompt_builder=PromptBuilder(),
            parser=OutputParser(repair_fn=None),
            intent_classifier=IntentClassifier(),
            router=ToolRouter(reg),
            executor=ToolExecutor(reg),
        )
    )
    # Either it finished (capped) or hit the step limit -> returns a string.
    assert isinstance(out["final_answer"], str)
