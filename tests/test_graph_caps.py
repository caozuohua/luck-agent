"""Task 5: resource caps (settings) + LangGraph HITL interrupt/resume."""
from __future__ import annotations

import asyncio
import json

from core.graph.engine import resume_graph, run_graph
from core.graph.state import AgentState
from core.intent_classifier import IntentClassifier
from core.output_parser import OutputParser
from core.prompt_builder import PromptBuilder
from core.router import ToolRouter
from core.supervisor import Supervisor
from core.tool_executor import ToolExecutor
from settings import load_settings
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


def test_settings_expose_graph_caps() -> None:
    s = load_settings()
    assert s.execution_mode in ("graph", "legacy")
    assert s.max_steps >= 1
    assert s.max_retry >= 0
    assert s.graph_db_path
    assert s.graph_max_active >= 1


class _FailTool(Tool):
    name = "general_search"
    description = "always fails"
    args_schema: dict = {}

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult.fail(error="boom", tool_name=self.name)


class _ActLLM:
    model = "act"

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return json.dumps(
            {"intent": "ACTION", "plan": "p", "fallback": "", "tool_call": {"name": "general_search", "args": {}}}
        )

    async def repair(self, raw: str, error: Exception, attempt: int) -> str:
        return raw


def _deps(reg: ToolRegistry) -> dict:
    return dict(
        llm=_ActLLM(),
        tools=reg,
        executor=ToolExecutor(reg),
        supervisor=Supervisor(),
        history="",
        prompt_builder=PromptBuilder(),
        parser=OutputParser(repair_fn=None),
        intent_classifier=IntentClassifier(),
        router=ToolRouter(reg),
        max_retry=0,  # force block on first failure -> interrupt()
    )


def _seed() -> AgentState:
    return AgentState(
        goal="do",
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


def test_block_triggers_interrupt(tmp_path) -> None:
    reg = ToolRegistry([_FailTool()])
    db = str(tmp_path / "hitl.db")
    out = asyncio.run(
        run_graph(
            _seed(),
            graph=None,
            config={"configurable": {"thread_id": "h1"}},
            max_steps=5,
            db_path=db,
            **_deps(reg),
        )
    )
    # A failing step with no retry budget blocks -> LangGraph pauses (HITL).
    assert "__interrupt__" in out


def test_resume_after_rejection_fails_gracefully(tmp_path) -> None:
    reg = ToolRegistry([_FailTool()])
    db = str(tmp_path / "hitl2.db")
    deps = _deps(reg)
    paused = asyncio.run(
        run_graph(
            _seed(),
            graph=None,
            config={"configurable": {"thread_id": "h2"}},
            max_steps=5,
            db_path=db,
            **deps,
        )
    )
    assert "__interrupt__" in paused

    resumed = asyncio.run(
        resume_graph("h2", {"approve": False}, db_path=db, max_steps=5, **deps)
    )
    assert resumed.get("decision") == "fail"
    assert "Blocked by operator" in resumed.get("final_answer", "")
