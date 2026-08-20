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
from core.operation_policy import operation_description, operation_requires_approval
from tools.base import ToolResult
from tools.registry import ToolNotFoundError, ToolRegistry

PatternWriter = Callable[..., Awaitable[Any]]
ApprovalChecker = Callable[[str, str, str, dict[str, Any]], bool]
AuditWriter = Callable[..., Awaitable[Any]]


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        timeout_seconds: float = 30.0,
        pattern_writer: PatternWriter | None = None,
        error_pattern_writer: PatternWriter | None = None,
        approval_checker: ApprovalChecker | None = None,
        audit_writer: AuditWriter | None = None,
    ) -> None:
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self.pattern_writer = pattern_writer or error_pattern_writer
        self.approval_checker = approval_checker
        self.audit_writer = audit_writer
        self._pending_patterns: list[asyncio.Task[None]] = []
        self._pending_audits: list[asyncio.Task[None]] = []

    async def execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        user_id: str = "",
        approval_token: str | None = None,
    ) -> ToolResult:
        started_at = time.perf_counter()
        args = args or {}
        # Small models sometimes emit a command name (ls, pwd, date, cat,
        # grep, find) as the tool name. Route those through the shell tool,
        # unless the name is an actually-registered tool.
        tool_name, args = self._normalize_tool_call(tool_name, args, self.registry)
        audited_operation: str | None = None
        if self.approval_checker is not None and operation_requires_approval(tool_name, args):
            operation = operation_description(tool_name, args)
            audited_operation = operation
            approved = False
            if approval_token:
                try:
                    approved = self.approval_checker(user_id, approval_token, tool_name, args)
                except Exception:
                    approved = False
            self._schedule_audit(
                user_id=user_id,
                tool_name=tool_name,
                operation=operation,
                decision="approved" if approved else "denied",
                details="approval_token_present=" + str(bool(approval_token)).lower(),
            )
            if not approved:
                result = ToolResult.fail(
                    error="APPROVAL_REQUIRED",
                    data={"operation": operation},
                    tool_name=tool_name,
                    metadata={"requires_approval": True},
                ).with_timing(started_at)
                self._schedule_pattern(tool_name, args, result, user_id=user_id)
                return result
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError:
            result = ToolResult.fail(
                error=f"TOOL_NOT_FOUND: {tool_name}",
                tool_name=tool_name,
            ).with_timing(started_at)
            self._schedule_result_audit(user_id, tool_name, audited_operation, result)
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
            self._schedule_result_audit(user_id, tool_name, audited_operation, result)
            self._schedule_pattern(tool_name, args, result, user_id=user_id)
            return result
        except Exception as exc:
            result = ToolResult.fail(
                error=str(exc) or exc.__class__.__name__,
                tool_name=tool_name,
            ).with_timing(started_at)
            self._schedule_result_audit(user_id, tool_name, audited_operation, result)
            self._schedule_pattern(tool_name, args, result, user_id=user_id)
            return result

        result.metadata.setdefault("tool_name", tool_name)
        self._schedule_result_audit(user_id, tool_name, audited_operation, result)
        self._schedule_pattern(tool_name, args, result, user_id=user_id)
        return result.with_timing(started_at)

    @staticmethod
    def _normalize_tool_call(
        tool_name: str, args: dict[str, Any], registry: ToolRegistry
    ) -> tuple[str, dict[str, Any]]:
        name = (tool_name or "").strip()
        # Already a real tool -> leave it untouched.
        if name in registry._tools:
            return name, args
        command_aliases = {
            "ls", "pwd", "date", "cat", "grep", "find", "df", "ps",
            "env", "whoami", "uname", "wc", "head", "tail", "tree",
        }
        if name in command_aliases:
            command = args.get("command") or args.get("cmd") or name
            rest = {k: v for k, v in args.items() if k not in ("command", "cmd")}
            return "shell", {"command": str(command), **rest}
        return name, args

    async def execute_model_output(
        self,
        raw_output: str,
        output_parser: OutputParser,
        *,
        user_id: str = "",
        approval_token: str | None = None,
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
            approval_token=approval_token,
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

    async def drain_pending_audits(self) -> None:
        while self._pending_audits:
            await asyncio.gather(*list(self._pending_audits))

    def _remove_pending_pattern(self, task: asyncio.Task[None]) -> None:
        try:
            self._pending_patterns.remove(task)
        except ValueError:
            pass

    def _schedule_audit(
        self,
        *,
        user_id: str,
        tool_name: str,
        operation: str,
        decision: str,
        details: str,
    ) -> None:
        if self.audit_writer is None:
            return

        async def write() -> None:
            try:
                await self.audit_writer(
                    user_id=user_id,
                    tool_name=tool_name,
                    operation=operation,
                    decision=decision,
                    details=details,
                )
            except Exception:
                return

        task = asyncio.create_task(write())
        self._pending_audits.append(task)
        task.add_done_callback(lambda done: self._remove_pending_audit(done))

    def _schedule_result_audit(
        self,
        user_id: str,
        tool_name: str,
        operation: str | None,
        result: ToolResult,
    ) -> None:
        if operation is None:
            return
        details = f"status={result.status}"
        if result.error:
            details += f" error={str(result.error)[:240]}"
        self._schedule_audit(
            user_id=user_id,
            tool_name=tool_name,
            operation=operation,
            decision="executed",
            details=details,
        )

    def _remove_pending_audit(self, task: asyncio.Task[None]) -> None:
        try:
            self._pending_audits.remove(task)
        except ValueError:
            pass
