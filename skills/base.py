from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from core.execution_engine import StepResult, StepSpec

ExecutionMode = Literal["goal_runtime", "legacy_inline"]


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    version: str
    intent: str
    description: str
    execution_mode: ExecutionMode
    priority: int = 100
    timeout: int = 120
    max_retry: int = 1
    required_permissions: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillContext:
    user_id: str
    chat_id: str
    text: str
    message_id: str = ""
    model_override: str = ""


@dataclass(frozen=True)
class SkillMatch:
    matched: bool
    score: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class GoalRequest:
    title: str
    intent: str
    success_criteria: tuple[str, ...] = ()
    plan: dict[str, Any] = field(default_factory=dict)


class Skill(Protocol):
    metadata: SkillMetadata

    def match(self, context: SkillContext) -> SkillMatch:
        ...


class GoalSkill(Skill, Protocol):
    def build_goal(self, context: SkillContext) -> GoalRequest:
        ...

    async def build_plan(self, goal: dict[str, Any]) -> list[StepSpec]:
        ...

    async def execute_step(
        self,
        goal: dict[str, Any],
        step: StepSpec,
    ) -> StepResult:
        ...

    async def is_goal_complete(
        self,
        goal: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> bool:
        ...
