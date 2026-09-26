from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from jsonschema.validators import validator_for

from memory.pattern_store import pattern_outcome_from_data
from memory.operation_store import OperationAttempt, OperationStore
from core.operation_context import current_operation
from core.output_parser import IntentType, OutputParser, ParseError
from core.operation_policy import (
    operation_description,
    operation_permission_applies,
    operation_requires_approval,
)
from tools.base import ToolResult
from tools.registry import ToolNotFoundError, ToolRegistry

PatternWriter = Callable[..., Awaitable[Any]]
ApprovalChecker = Callable[[str, str, str, dict[str, Any]], bool]
PermissionChecker = Callable[[str, str, dict[str, Any]], bool]
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
        permission_checker: PermissionChecker | None = None,
        audit_writer: AuditWriter | None = None,
        operation_store: OperationStore | None = None,
    ) -> None:
        self.registry = registry
        self.timeout_seconds = timeout_seconds
        self.pattern_writer = pattern_writer or error_pattern_writer
        self.approval_checker = approval_checker
        self.permission_checker = permission_checker
        self.audit_writer = audit_writer
        self.operation_store = operation_store
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
        if self.operation_store is None:
            return await self._execute(tool_name, args, user_id=user_id, approval_token=approval_token)

        normalized, normalized_args = tool_name, args
        if isinstance(args, dict) or args is None:
            normalized, normalized_args = self._normalize_tool_call(tool_name, args or {}, self.registry)
        try:
            tool = self.registry.get(normalized)
        except ToolNotFoundError:
            tool = None
        try:
            attempt = await self.operation_store.prepare(
                context=current_operation.get(), user_id=user_id,
                requested_tool=tool_name, tool_name=normalized,
                arguments=normalized_args, schema=getattr(tool, "args_schema", {}),
                may_have_effect=getattr(tool, "effect", "unknown") != "read",
            )
        except Exception:
            return ToolResult.fail(
                error="OPERATION_AUDIT_UNAVAILABLE", tool_name=normalized,
                metadata={"blocking": True, "executed": False, "error_class": "audit_unavailable"},
            )
        try:
            result = await self._execute(
                tool_name, args, user_id=user_id, approval_token=approval_token, attempt=attempt,
            )
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(asyncio.shield(attempt.finish(
                    state="unknown" if attempt.started else "cancelled", error_class="cancelled",
                )), timeout=2)
            except (Exception, asyncio.CancelledError):
                pass  # The durable executing record remains unresolved.
            raise
        except Exception:
            result = ToolResult.fail(
                error="EXECUTION_OUTCOME_UNKNOWN" if attempt.started else "EXECUTION_REJECTED",
                metadata={"blocking": True, "error_class": "internal"},
            )
        result.metadata.update({"attempt_id": attempt.attempt_id, "operation_id": attempt.operation_id})
        uncertain = attempt.started and (
            result.error in {"TIMEOUT_ERROR", "EXECUTION_OUTCOME_UNKNOWN"}
            or result.metadata.get("execution_uncertain", False)
        )
        if uncertain:
            result.metadata.update({"execution_uncertain": True, "blocking": True})
        state = ("unknown" if uncertain else "returned_ok" if result.status == "ok"
                 else "returned_error" if attempt.started else "rejected")
        try:
            await attempt.finish(state=state, error_class=str(result.metadata.get("error_class") or ""))
        except Exception:
            return ToolResult.fail(
                error="OPERATION_RESULT_NOT_PERSISTED", tool_name=normalized,
                metadata={"blocking": True, "execution_uncertain": attempt.started,
                          "attempt_id": attempt.attempt_id, "operation_id": attempt.operation_id},
            )
        return result

    async def _execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        user_id: str = "",
        approval_token: str | None = None,
        attempt: OperationAttempt | None = None,
    ) -> ToolResult:
        started_at = time.perf_counter()
        args = {} if args is None else args
        if not isinstance(args, dict):
            return ToolResult.fail(
                error="INVALID_TOOL_ARGUMENTS",
                tool_name=tool_name,
                metadata={"error_class": "invalid_arguments", "validation_rules": ["type"], "executed": False},
            ).with_timing(started_at)
        try:
            # Python's JSON encoder otherwise accepts NaN/Infinity, which do
            # not belong to the wire contract and can evade numeric bounds.
            json.dumps(args, allow_nan=False)
            if any(not isinstance(key, str) for key in args):
                raise ValueError("argument keys must be strings")
        except (TypeError, ValueError, OverflowError, RecursionError):
            return ToolResult.fail(
                error="INVALID_TOOL_ARGUMENTS",
                tool_name=tool_name,
                metadata={"error_class": "invalid_arguments", "validation_rules": ["json"], "executed": False},
            ).with_timing(started_at)
        # Small models sometimes emit a command name (ls, pwd, date, cat,
        # grep, find) as the tool name. Route those through the shell tool,
        # unless the name is an actually-registered tool.
        tool_name, args = self._normalize_tool_call(tool_name, args, self.registry)
        audited_operation: str | None = None
        try:
            tool = self.registry.get(tool_name)
        except ToolNotFoundError:
            return ToolResult.fail(
                error=f"TOOL_NOT_FOUND: {tool_name}",
                tool_name=tool_name,
                metadata={"error_class": "unknown_tool", "executed": False},
            ).with_timing(started_at)

        # Validate before consuming a one-shot approval or invoking tool code.
        # Return only schema rules, never rejected values or exception messages.
        validator = validator_for(tool.args_schema)(tool.args_schema)
        rules = sorted({str(error.validator) for error in validator.iter_errors(args)})
        if rules:
            self._schedule_audit(
                user_id=user_id, tool_name=tool_name, operation="validate_arguments",
                decision="arguments_invalid", details=",".join(rules),
            )
            return ToolResult.fail(
                error="INVALID_TOOL_ARGUMENTS",
                tool_name=tool_name,
                metadata={"error_class": "invalid_arguments", "validation_rules": rules, "executed": False},
            ).with_timing(started_at)
        if self.permission_checker is not None and operation_permission_applies(tool_name, args):
            permitted = False
            try:
                permitted = self.permission_checker(user_id, tool_name, args)
            except Exception:
                permitted = False
            if not permitted:
                operation = operation_description(tool_name, args)
                self._schedule_audit(
                    user_id=user_id,
                    tool_name=tool_name,
                    operation=operation,
                    decision="permission_denied",
                    details="target/service/operation policy rejected",
                )
                result = ToolResult.fail(
                    error="PERMISSION_DENIED",
                    data={"operation": operation},
                    tool_name=tool_name,
                    metadata={"permission_denied": True},
                ).with_timing(started_at)
                self._schedule_pattern(tool_name, args, result, user_id=user_id)
                return result
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
        if attempt is not None:
            try:
                await attempt.executing(approved=audited_operation is not None)
            except Exception:
                return ToolResult.fail(
                    error="OPERATION_AUDIT_UNAVAILABLE", tool_name=tool_name,
                    metadata={"blocking": True, "executed": False, "execution_uncertain": True},
                )
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
                metadata={"execution_uncertain": True, "error_class": type(exc).__name__},
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
            command = args["command"] if "command" in args else args.get("cmd", name)
            rest = {k: v for k, v in args.items() if k not in ("command", "cmd")}
            return "shell", {"command": command, **rest}
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
