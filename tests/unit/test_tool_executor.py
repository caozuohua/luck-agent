from __future__ import annotations

import asyncio

import pytest

from core.output_parser import IntentType, OutputParser, ParseError
from core.tool_executor import ToolExecutor
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


class OkTool(Tool):
    name = "ok"
    description = "ok"
    args_schema = {}

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult.ok(data={"value": kwargs.get("value")}, tool_name=self.name)


class FailingTool(Tool):
    name = "fail"
    description = "fail"
    args_schema = {}

    async def run(self, **kwargs: object) -> ToolResult:
        raise RuntimeError("boom")


class SlowTool(Tool):
    name = "slow"
    description = "slow"
    args_schema = {}

    async def run(self, **kwargs: object) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult.ok(tool_name=self.name)


@pytest.mark.asyncio
async def test_normal_execution_returns_ok_result() -> None:
    result = await ToolExecutor(ToolRegistry([OkTool()])).execute("ok", {"value": 3})

    assert result.status == "ok"
    assert result.data == {"value": 3}


@pytest.mark.asyncio
async def test_tool_exception_returns_error_result() -> None:
    result = await ToolExecutor(ToolRegistry([FailingTool()])).execute("fail", {})

    assert result.status == "error"
    assert result.error == "boom"


@pytest.mark.asyncio
async def test_timeout_returns_timeout_error() -> None:
    result = await ToolExecutor(ToolRegistry([SlowTool()]), timeout_seconds=0.01).execute("slow", {})

    assert result.status == "error"
    assert result.error == "TIMEOUT_ERROR"


@pytest.mark.asyncio
async def test_schema_failure_triggers_repair_and_retry_before_execute() -> None:
    async def repair(raw_output: str, error: ParseError, attempt: int) -> str:
        return '{"intent":"ACTION","plan":"run","tool_call":{"name":"ok","args":{"value":5}},"fallback":"stop"}'

    result = await ToolExecutor(ToolRegistry([OkTool()])).execute_model_output(
        '{"intent":"ACTION"}',
        OutputParser(repair_fn=repair),
    )

    assert result.status == "ok"
    assert result.data == {"value": 5}
