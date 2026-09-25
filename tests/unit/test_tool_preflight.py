from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from core.tool_executor import ToolExecutor
from tools.base import ToolResult
from tools.registry import ToolRegistrationError, ToolRegistry
from tools.shell import ShellTool
from tools.web_search import WebSearchTool


@pytest.mark.parametrize("tool,args", [
    (ShellTool, {}),
    (ShellTool, {"command": ""}),
    (ShellTool, {"command": " \n "}),
    (ShellTool, {"command": 123}),
    (ShellTool, {"command": "pwd", "timeout": 0}),
    (ShellTool, {"command": "pwd", "timeout": True}),
    (ShellTool, {"command": "pwd", "timeout": 30}),
    (ShellTool, {"command": "pwd", "timeout": float("nan")}),
    (ShellTool, {"command": "pwd", "timeout": float("inf")}),
    (ShellTool, {"command": "pwd", "target": "other-host"}),
    (WebSearchTool, {}),
    (WebSearchTool, {"query": " "}),
    (WebSearchTool, {"query": "weather", "num_results": "5"}),
    (WebSearchTool, {"query": "weather", "num_results": 11}),
    (WebSearchTool, {"query": "weather", "num_results": False}),
    (WebSearchTool, {"query": object()}),
])
@pytest.mark.asyncio
async def test_invalid_arguments_never_execute_or_consume_approval(tool, args):
    instance = tool()
    instance.run = AsyncMock(return_value=ToolResult.ok())
    approval = Mock(return_value=True)
    executor = ToolExecutor(ToolRegistry([instance]), approval_checker=approval)
    result = await executor.execute(instance.name, args, approval_token="one-shot")
    assert result.error == "INVALID_TOOL_ARGUMENTS"
    assert result.metadata["executed"] is False
    instance.run.assert_not_called()
    approval.assert_not_called()


@pytest.mark.parametrize("args", [False, 0, [], "credential-do-not-echo"])
@pytest.mark.asyncio
async def test_non_object_arguments_are_rejected_without_echo(args):
    result = await ToolExecutor(ToolRegistry([ShellTool()])).execute("shell", args)
    assert result.error == "INVALID_TOOL_ARGUMENTS"
    assert "credential-do-not-echo" not in str(result.to_dict())


@pytest.mark.asyncio
async def test_corrected_call_executes_once_without_validation_echo():
    tool = ShellTool()
    tool.run = AsyncMock(return_value=ToolResult.ok(tool_name="shell"))
    executor = ToolExecutor(ToolRegistry([tool]))
    assert (await executor.execute("shell", {"command": {"secret": "do-not-echo"}})).error == "INVALID_TOOL_ARGUMENTS"
    result = await executor.execute("pwd", {})
    assert result.status == "ok"
    tool.run.assert_awaited_once_with(command="pwd")


@pytest.mark.parametrize("command", [0, "", None, False])
@pytest.mark.asyncio
async def test_alias_does_not_replace_an_explicit_invalid_command(command):
    tool = ShellTool()
    tool.run = AsyncMock()
    result = await ToolExecutor(ToolRegistry([tool])).execute("pwd", {"command": command})
    assert result.error == "INVALID_TOOL_ARGUMENTS"
    tool.run.assert_not_called()


def test_invalid_schema_cannot_register():
    tool = ShellTool()
    tool.args_schema = {"type": "imaginary"}
    with pytest.raises(ToolRegistrationError, match="invalid argument schema"):
        ToolRegistry([tool])
