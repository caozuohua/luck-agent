"""
core/execution_engine.py — Luck-Agent 2.0 standardized execution engine.

The engine is intentionally generic:
- GoalManager owns lifecycle and persistence.
- ExecutionEngine owns loop control, retries, and dispatch.
- Domain controllers own actual business steps, e.g. BlogController.

A controller only needs to implement a small async protocol:
    build_plan(goal) -> list[StepSpec]
    execute_step(goal, step) -> StepResult
    is_goal_complete(goal, steps) -> bool

BlogWrite will be the first controller, but the same engine can later run
GitHub code edits, shell maintenance, PKB reviews, deployment diagnosis, etc.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from core.log import get_logger
from core.protocols import ToolResult

log = get_logger()


@dataclass
class StepSpec:
    """Declarative step definition produced by a domain controller."""

    name: str
    action: str
    input: dict[str, Any] = field(default_factory=dict)
    required: bool = True
    max_retry: int = 1
    timeout: int = 120

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StepResult:
    """Normalized execution result returned by controllers."""

    ok: bool
    action: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    hint: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    next_step: str = ""
    blocking: bool = False
    elapsed_ms: int = 0

    def to_tool_result(self, tool: str = "controller") -> dict[str, Any]:
        return ToolResult(
            ok=self.ok,
            tool=tool,
            action=self.action,
            data=self.data,
            error=self.error or None,
            hint=self.hint or None,
            artifacts=self.artifacts,
            elapsed_ms=self.elapsed_ms,
        ).to_dict()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GoalController(Protocol):
    """Controller interface for one goal intent/domain."""

    intent: str

    async def build_plan(self, goal: dict) -> list[StepSpec]:
        """Return a deterministic step plan for a goal."""
        ...

    async def execute_step(self, goal: dict, step: StepSpec) -> StepResult:
        """Execute one step and return a normalized result."""
        ...

    async def is_goal_complete(self, goal: dict, steps: list[dict]) -> bool:
        """Return whether the goal meets completion criteria."""
        ...


class ExecutionEngineError(RuntimeError):
    """Execution engine error."""


class ExecutionEngine:
    """Generic execution loop for persisted goals."""

    def __init__(
        self,
        *,
        goal_manager,
        controllers: dict[str, GoalController] | None = None,
        max_steps: int = 30,
        default_step_timeout: int = 120,
    ) -> None:
        self.goal_manager = goal_manager
        self.controllers: dict[str, GoalController] = controllers or {}
        self.max_steps = max_steps
        self.default_step_timeout = default_step_timeout

    def register_controller(self, controller: GoalController) -> None:
        self.controllers[controller.intent] = controller
        log.info("controller_registered", intent=controller.intent)

    def get_controller(self, intent: str) -> GoalController:
        controller = self.controllers.get(intent)
        if not controller:
            raise ExecutionEngineError(f"no controller registered for intent: {intent}")
        return controller

    async def run_goal(self, goal_id: str) -> dict:
        """Run a goal until done, blocked, failed, cancelled, or step budget exhausted."""
        goal = self.goal_manager.start_goal(goal_id)
        controller = self.get_controller(goal.get("intent", "general"))
        await self._ensure_plan(goal, controller)

        for _ in range(self.max_steps):
            goal = self.goal_manager.get_goal(goal_id)
            if goal.get("status") in {"done", "failed", "cancelled", "blocked"}:
                return goal

            steps = self.goal_manager.get_steps(goal_id)
            if await controller.is_goal_complete(goal, steps):
                return self.goal_manager.complete_goal(goal_id)

            step_record = self._next_pending_step(steps)
            if not step_record:
                return self.goal_manager.block_goal(goal_id, "no pending step but goal is incomplete")

            result = await self.run_step(goal_id, step_record["step_id"])
            if not result.ok:
                if result.blocking:
                    return self.goal_manager.block_goal(goal_id, result.error or "step blocked", step_record.get("name"))
                retry_count = int(step_record.get("retry_count") or 0)
                max_retry = int((step_record.get("input") or {}).get("max_retry") or 0)
                if retry_count < max_retry:
                    self.goal_manager.memory.update_goal_step(
                        step_record["step_id"],
                        status="pending",
                        retry_count=retry_count + 1,
                        error=result.error,
                    )
                    continue
                return self.goal_manager.block_goal(goal_id, result.error or "step failed", step_record.get("name"))

        return self.goal_manager.block_goal(goal_id, f"max step budget exceeded: {self.max_steps}")

    async def run_step(self, goal_id: str, step_id: str) -> StepResult:
        """Run one persisted step through the registered controller."""
        goal = self.goal_manager.get_goal(goal_id)
        step_record = self.goal_manager.memory.get_goal_step(step_id)
        if not step_record:
            raise ExecutionEngineError(f"step not found: {step_id}")
        controller = self.get_controller(goal.get("intent", "general"))
        step = self._step_spec_from_record(step_record)

        self.goal_manager.start_step(step_id)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                controller.execute_step(goal, step),
                timeout=step.timeout or self.default_step_timeout,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result = StepResult(
                ok=False,
                action=step.action,
                error=f"step timeout after {step.timeout or self.default_step_timeout}s",
                blocking=True,
                elapsed_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            result = StepResult(
                ok=False,
                action=step.action,
                error=f"{type(e).__name__}: {e}",
                blocking=True,
                elapsed_ms=elapsed_ms,
            )

        if result.elapsed_ms <= 0:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)

        output = result.to_dict()
        if result.ok:
            self.goal_manager.finish_step(step_id, output=output)
            for artifact in result.artifacts:
                self.goal_manager.append_artifact(goal_id, artifact)
        else:
            self.goal_manager.fail_step(step_id, result.error, output=output)
        log.info(
            "goal_step_executed",
            goal_id=goal_id,
            step_id=step_id,
            action=step.action,
            ok=result.ok,
            elapsed_ms=result.elapsed_ms,
        )
        return result

    async def _ensure_plan(self, goal: dict, controller: GoalController) -> None:
        """Create persisted goal steps if the goal has no step records yet."""
        goal_id = goal["goal_id"]
        existing_steps = self.goal_manager.get_steps(goal_id)
        if existing_steps:
            return
        plan = await controller.build_plan(goal)
        if not plan:
            raise ExecutionEngineError(f"controller returned empty plan: {controller.intent}")
        for step in plan:
            payload = step.to_dict()
            payload["max_retry"] = step.max_retry
            self.goal_manager.create_step(
                goal_id=goal_id,
                name=step.name,
                input=payload,
                status="pending",
            )
        self.goal_manager.set_current_step(goal_id, plan[0].name)

    @staticmethod
    def _next_pending_step(steps: list[dict]) -> dict | None:
        for step in steps:
            if step.get("status") == "pending":
                return step
        return None

    @staticmethod
    def _step_spec_from_record(step_record: dict) -> StepSpec:
        raw = step_record.get("input") or {}
        return StepSpec(
            name=step_record.get("name") or raw.get("name") or "unnamed_step",
            action=raw.get("action") or step_record.get("name") or "unknown_action",
            input=raw.get("input") or {},
            required=bool(raw.get("required", True)),
            max_retry=int(raw.get("max_retry", 1)),
            timeout=int(raw.get("timeout", 120)),
        )
