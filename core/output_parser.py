from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class IntentType(Enum):
    ACTION = "ACTION"
    CHAT = "CHAT"
    CLARIFY = "CLARIFY"
    CANNOT_COMPLETE = "CANNOT_COMPLETE"


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ParsedOutput:
    intent: IntentType
    plan: str = ""
    tool_call: ToolCall | None = None
    fallback: str = ""
    message: str = ""
    question: str = ""
    best_guess: str = ""
    reason: str = ""
    suggestion: str = ""


RepairFn = Callable[[str, ParseError, int], Awaitable[str]]


class OutputParser:
    def __init__(
        self,
        *,
        repair_fn: RepairFn | None = None,
        max_retries: int = 2,
    ) -> None:
        self._repair_fn = repair_fn
        self._max_retries = max_retries

    def parse(self, raw_output: str) -> ParsedOutput:
        payload = self._loads(raw_output)
        intent_raw = payload.get("intent")
        if not isinstance(intent_raw, str) or not intent_raw:
            # No intent field at all -> treat as a plain chat reply if a
            # message is present, else fail gracefully.
            if isinstance(payload.get("message"), str) and payload["message"].strip():
                return self._parse_chat(payload)
            raise ParseError("intent is required")
        try:
            intent = IntentType(intent_raw)
        except ValueError:
            # Small models invent intent names (e.g. GET_CURRENT_DIRECTORY,
            # ask_date). Map heuristically instead of hard-failing the turn.
            intent = self._map_unknown_intent(intent_raw, payload)
        if intent is IntentType.ACTION:
            return self._parse_action(payload)
        if intent is IntentType.CHAT:
            return self._parse_chat(payload)
        if intent is IntentType.CLARIFY:
            return self._parse_clarify(payload)
        return self._parse_cannot_complete(payload)

    @staticmethod
    def _map_unknown_intent(raw: str, payload: dict[str, Any]) -> IntentType:
        lowered = raw.lower()
        action_signals = (
            "action", "tool", "run", "exec", "command", "shell", "date", "time",
            "dir", "directory", "pwd", "ls", "cat", "search", "fetch", "deploy",
            "schedule", "create", "write", "read", "call", "invoke",
        )
        if any(sig in lowered for sig in action_signals):
            return IntentType.ACTION
        if any(sig in lowered for sig in ("clarif", "question", "unsure", "unknown")):
            return IntentType.CLARIFY
        if any(sig in lowered for sig in ("cannot", "fail", "error", "unable")):
            return IntentType.CANNOT_COMPLETE
        # A tool_call is present -> clearly meant ACTION.
        if isinstance(payload.get("tool_call"), dict):
            return IntentType.ACTION
        return IntentType.CHAT

    async def repair_and_retry(self, raw_output: str, error: ParseError) -> ParsedOutput:
        current_output = raw_output
        current_error = error
        for attempt in range(1, self._max_retries + 1):
            if self._repair_fn is None:
                break
            try:
                current_output = await self._repair_fn(current_output, current_error, attempt)
                return self.parse(current_output)
            except ParseError as exc:
                current_error = exc
        return ParsedOutput(
            intent=IntentType.CANNOT_COMPLETE,
            reason=f"LLM output schema validation failed: {current_error}",
            suggestion="Please retry with a more specific request.",
        )

    def _loads(self, raw_output: str) -> dict[str, Any]:
        cleaned = self._strip_markdown_fence(raw_output)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ParseError(f"invalid JSON: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ParseError("top-level output must be a JSON object")
        return payload

    def _strip_markdown_fence(self, raw_output: str) -> str:
        text = (raw_output or "").strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return text

    def _require_str(self, payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ParseError(f"{key} is required")
        return value.strip()

    def _parse_action(self, payload: dict[str, Any]) -> ParsedOutput:
        # plan / fallback are OPTIONAL: a model may legitimately return an
        # empty fallback (no escape hatch). Only intent + a well-formed
        # tool_call are required, so empty strings don't force a needless
        # (and often failing) repair retry.
        plan = payload.get("plan")
        plan = plan if isinstance(plan, str) else ""
        fallback = payload.get("fallback")
        fallback = fallback if isinstance(fallback, str) else ""
        tool_call = payload.get("tool_call")
        if not isinstance(tool_call, dict):
            raise ParseError("tool_call object is required")
        name = tool_call.get("name")
        # Small models drift on the key name for arguments: prefer `args`,
        # fall back to `arguments` or OpenAI-style `parameters` only when
        # `args` is absent (so an explicitly non-object `args` is still caught).
        if "args" in tool_call:
            args = tool_call.get("args")
        else:
            args = tool_call.get("arguments") or tool_call.get("parameters") or {}
        if not isinstance(name, str) or not name.strip():
            raise ParseError("tool_call.name is required")
        if not isinstance(args, dict):
            raise ParseError("tool_call args must be an object")
        return ParsedOutput(
            intent=IntentType.ACTION,
            plan=plan.strip(),
            tool_call=ToolCall(name=name.strip(), args=args),
            fallback=fallback.strip(),
        )

    def _parse_chat(self, payload: dict[str, Any]) -> ParsedOutput:
        return ParsedOutput(
            intent=IntentType.CHAT,
            message=self._require_str(payload, "message"),
        )

    def _parse_clarify(self, payload: dict[str, Any]) -> ParsedOutput:
        return ParsedOutput(
            intent=IntentType.CLARIFY,
            question=self._require_str(payload, "question"),
            best_guess=self._require_str(payload, "best_guess"),
        )

    def _parse_cannot_complete(self, payload: dict[str, Any]) -> ParsedOutput:
        return ParsedOutput(
            intent=IntentType.CANNOT_COMPLETE,
            reason=self._require_str(payload, "reason"),
            suggestion=self._require_str(payload, "suggestion"),
        )
