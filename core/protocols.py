"""
core/protocols.py — Luck-Agent 2.0 structured JSON protocols.

Internal protocol objects are dataclass-based to keep dependencies light on e2-micro.
jsonschema is used only for optional validation at runtime boundaries.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

try:
    from jsonschema import Draft202012Validator
except Exception:  # pragma: no cover - allows old envs to boot before pip install
    Draft202012Validator = None  # type: ignore

GoalStatus = Literal["pending", "running", "done", "failed", "blocked", "interrupted", "cancelled"]
StepStatus = Literal["pending", "running", "done", "failed", "skipped", "blocked"]
VerificationVerdict = Literal["passed", "failed", "blocked", "continue"]


def now_ts() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class ArtifactRef:
    type: str
    path: str = ""
    url: str = ""
    title: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolResult:
    ok: bool
    tool: str
    action: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    hint: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Goal:
    goal_id: str
    user_id: str
    chat_id: str
    title: str
    intent: str
    status: GoalStatus = "pending"
    success_criteria: list[str] = field(default_factory=list)
    current_step: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GoalStep:
    step_id: str
    goal_id: str
    name: str
    status: StepStatus = "pending"
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    retry_count: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    created_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationResult:
    verdict: VerificationVerdict
    is_goal_complete: bool = False
    reason: str = ""
    next_action: str = ""
    blocking: bool = False
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Lesson:
    domain: str
    task_type: str
    error_pattern: str
    solution: str
    root_cause: str = ""
    prevention: str = ""
    confidence: float = 0.5
    lesson_id: int | None = None
    use_count: int = 0
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


GOAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal_id", "user_id", "chat_id", "title", "intent", "status"],
    "properties": {
        "goal_id": {"type": "string"},
        "user_id": {"type": "string"},
        "chat_id": {"type": "string"},
        "title": {"type": "string"},
        "intent": {"type": "string"},
        "status": {"enum": ["pending", "running", "done", "failed", "blocked", "interrupted", "cancelled"]},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "current_step": {"type": "string"},
        "plan": {"type": "object"},
        "artifacts": {"type": "array"},
        "error": {"type": "string"},
    },
}

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["step_id", "goal_id", "name", "status"],
    "properties": {
        "step_id": {"type": "string"},
        "goal_id": {"type": "string"},
        "name": {"type": "string"},
        "status": {"enum": ["pending", "running", "done", "failed", "skipped", "blocked"]},
        "input": {"type": "object"},
        "output": {"type": "object"},
        "error": {"type": "string"},
        "retry_count": {"type": "integer"},
    },
}

TOOL_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ok", "tool", "data"],
    "properties": {
        "ok": {"type": "boolean"},
        "tool": {"type": "string"},
        "action": {"type": "string"},
        "data": {"type": "object"},
        "error": {"type": ["string", "null"]},
        "hint": {"type": ["string", "null"]},
        "artifacts": {"type": "array"},
        "elapsed_ms": {"type": "integer"},
    },
}

VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "is_goal_complete", "blocking"],
    "properties": {
        "verdict": {"enum": ["passed", "failed", "blocked", "continue"]},
        "is_goal_complete": {"type": "boolean"},
        "reason": {"type": "string"},
        "next_action": {"type": "string"},
        "blocking": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
}


def validate_json(payload: dict[str, Any], schema: dict[str, Any]) -> tuple[bool, str]:
    """Validate a protocol payload. Returns (ok, error)."""
    if Draft202012Validator is None:
        return True, "jsonschema not installed; validation skipped"
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: e.path)
    if not errors:
        return True, ""
    first = errors[0]
    path = ".".join(str(p) for p in first.path)
    return False, f"{path}: {first.message}" if path else first.message
