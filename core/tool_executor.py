from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from memory.pattern_store import pattern_outcome_from_data
from core.output_parser import IntentType, OutputParser, ParseError
from tools.base import ToolResult
from tools.registry import ToolNotFoundError, ToolRegistry

PatternWriter = Callable[..., Awaitable[Any]]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        timeout_seconds: float = 30.0,
        pattern_writer: PatternWriter | None = None,
        error_pattern_writer: PatternWriter | None = None,
    ) -> None:
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self.pattern_writer = pattern_writer or error_pattern_writer
        self._pending_patterns: list[asyncio.Task[None]] = []

    async def execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        user_id: str = "",
    ) -> ToolResult:
        started_at = time.perf_counter()
        args = args or {}
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError:
            result = ToolResult.fail(
                error=f"TOOL_NOT_FOUND: {tool_name}",
                tool_name=tool_name,
            ).with_timing(started_at)
            self._schedule_pattern(tool_name, args, result, user_id=user_id)
            return result

        try:
            result = await asyncio.wait_for(
                self._run_tool(tool, args),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            result = ToolResult.fail(
                error="TIMEOUT_ERROR",
                tool_name=tool_name,
            ).with_timing(started_at)
            self._schedule_pattern(tool_name, args, result, user_id=user_id)
            return result
        except Exception as exc:
            result = ToolResult.fail(
                error=str(exc) or exc.__class__.__name__,
                tool_name=tool_name,
            ).with_timing(started_at)
            self._schedule_pattern(tool_name, args, result, user_id=user_id)
            return result

        result.metadata.setdefault("tool_name", tool_name)
        self._schedule_pattern(tool_name, args, result, user_id=user_id)
        return result.with_timing(started_at)

    async def execute_model_output(
        self,
        raw_output: str,
        output_parser: OutputParser,
        *,
        user_id: str = "",
    ) -> ToolResult:
        try:
            parsed = output_parser.parse(raw_output)
        except ParseError as exc:
            parsed = await output_parser.repair_and_retry(raw_output, exc)
        if parsed.intent is not IntentType.ACTION or parsed.tool_call is None:
            return ToolResult.fail(
                error="MODEL_OUTPUT_NOT_ACTION",
                data={"intent": parsed.intent.value},
            )
        return await self.execute(
            parsed.tool_call.name,
            parsed.tool_call.args,
            user_id=user_id,
        )

    async def _run_tool(self, tool: Any, args: dict[str, Any]) -> ToolResult:
        value = tool.run(**args)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, ToolResult):
            return ToolResult.fail(
                error="INVALID_TOOL_RESULT",
                tool_name=getattr(tool, "name", ""),
                data=value,
            )
        return value

    def _schedule_pattern(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: ToolResult,
        user_id: str = "",
    ) -> None:
        if self.pattern_writer is None:
            return
        pattern_type = "success" if result.status == "ok" else "error"
        outcome = result.error or pattern_outcome_from_data(result.data)
        task = asyncio.create_task(
            self.pattern_writer(
                pattern_type=pattern_type,
                pattern_id=uuid.uuid4().hex,
                trigger=f"tool execution completed: {tool_name}",
                tool_name=tool_name,
                args_schema=json.dumps(args, ensure_ascii=False, sort_keys=True),
                outcome=outcome,
                user_id=user_id,
            )
        )
        self._pending_patterns.append(task)
        task.add_done_callback(lambda done: self._remove_pending_pattern(done))

    async def drain_pending_patterns(self) -> None:
        while self._pending_patterns:
            await asyncio.gather(*list(self._pending_patterns))

    def _remove_pending_pattern(self, task: asyncio.Task[None]) -> None:
        try:
            self._pending_patterns.remove(task)
        except ValueError:
            pass
