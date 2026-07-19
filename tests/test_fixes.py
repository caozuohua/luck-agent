"""Fix-verification tests: tool whitelist + non-empty failure answers."""
from __future__ import annotations

import json

from tools.base import Tool, ToolResult
from tools.shell import ShellTool


def test_readonly_commands_allowed() -> None:
    # "查看日期" type requests must not be rejected at the prefix gate.
    tool = ShellTool()
    for cmd in ("date", "cal", "whoami", "uname", "hostname"):
        assert tool.validate_command(cmd) == "", f"{cmd} should be allowed"
    # dangerous patterns are rejected (different wording than prefix gate)
    assert tool.validate_command("rm -rf /") != ""
    # unknown prefixes are rejected at the prefix gate
    assert "not allowed" in tool.validate_command("nmap 1.2.3.4")


def test_windows_date_rewrites_to_powershell() -> None:
    # On Windows the shell is cmd.exe where `date` is interactive; the tool
    # must rewrite it to a read-only PowerShell query so it actually works.
    import os

    tool = ShellTool()
    if os.name == "nt":
        rewritten = tool._platform_command("date")
        assert "powershell" in rewritten and "Get-Date" in rewritten
        # and the rewritten command is itself whitelisted
        assert tool.validate_command(rewritten) == ""
    else:
        # On POSIX, `date` is used as-is.
        assert tool._platform_command("date") == "date"


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


async def test_failed_tool_path_returns_nonempty_answer() -> None:
    from core.agent import MinimalAgent
    from core.router import ToolRouter
    from memory.db import Database
    from memory.goal_store import GoalStore
    from tools.registry import ToolRegistry

    db = Database(":memory:")
    await db.initialize()
    reg = ToolRegistry([_FailTool()])
    agent = MinimalAgent(
        llm_client=_ActLLM(),
        tool_registry=reg,
        router=ToolRouter(reg),
        goal_store=GoalStore(db),
        execution_mode="graph",
        max_retry=0,  # force block -> fail (no HITL in web path)
    )
    answer = await agent.run_turn("do something", user_id="u1")
    assert answer, "must not be empty"
    assert "未生成回复" not in answer, "must not surface the empty-reply placeholder"
    assert "boom" in answer or "执行被阻断" in answer or "未能完成" in answer
