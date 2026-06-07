"""
core/execution_engine.py — Luck-Agent 2.0 standardized execution engine.

The engine is generic:
- GoalManager owns lifecycle and persistence.
- ExecutionEngine owns loop control and dispatch.
- Supervisor owns verification, retry/block decisions, and lesson capture.
- Domain controllers own actual business steps, e.g. BlogController.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from core.log import get_logger
from core.protocols import ToolResult
from core.supervisor import Supervisor, SupervisorDecision

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
        ...

    async def execute_step(self, goal: dict, step: StepSpec) -> StepResult:
        ...

    async def is_goal_complete(self, goal: dict, steps: list[dict]) -> bool:
        ...


class ExecutionEngineError(RuntimeError):
    """Execution engine error."""


class ExecutionEngine:
    """Generic execution loop for persisted goals."""

    def __init__(
        self,
        *,
        goal_manager,
        supervisor: Supervisor | None = None,
        controllers: dict[str, GoalController] | None = None,
        max_steps: int = 30,
        default_step_timeout: int = 120,
    ) -> None:
        self.goal_manager = goal_manager
        self.supervisor = supervisor or Supervisor(memory=getattr(goal_manager, "memory", None))
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
            complete = await controller.is_goal_complete(goal, steps)
            completion = self.supervisor.review_goal_completion(goal=goal, steps=steps, complete=complete)
            if completion.get("is_goal_complete"):
                return self.goal_manager.complete_goal(goal_id)

            step_record = self._next_pending_step(steps)
            if not step_record:
                return self.goal_manager.block_goal(goal_id, "no pending step but goal is incomplete")

            decision = await self.run_step(goal_id, step_record["step_id"])
            if decision.decision == "pass":
                continue
            if decision.decision == "retry":
                self._mark_step_for_retry(step_record, decision)
                continue
            if decision.decision == "fail":
                return self.goal_manager.fail_goal(goal_id, decision.reason or "step failed")
            return self.goal_manager.block_goal(
                goal_id,
                decision.reason or "step blocked",
                step_record.get("name"),
            )

        return self.goal_manager.block_goal(goal_id, f"max step budget exceeded: {self.max_steps}")

    async def run_step(self, goal_id: str, step_id: str) -> SupervisorDecision:
        """Run one persisted step and let Supervisor decide pass/retry/block/fail."""
        goal = self.goal_manager.get_goal(goal_id)
        step_record = self.goal_manager.memory.get_goal_step(step_id)
        if not step_record:
            raise ExecutionEngineError(f"step not found: {step_id}")
        controller = self.get_controller(goal.get("intent", "general"))
        step = self._step_spec_from_record(step_record)

        self.goal_manager.start_step(step_id)
        result = await self._execute_controller_step(controller, goal, step)

        retry_count = int(step_record.get("retry_count") or 0)
        max_retry = int((step_record.get("input") or {}).get("max_retry") or step.max_retry)
        decision = self.supervisor.review_step_result(
            goal=goal,
            step=step_record,
            result=result,
            retry_count=retry_count,
            max_retry=max_retry,
        )

        output = {
            "result": result.to_dict(),
            "supervisor": decision.to_dict(),
        }
        if decision.decision == "pass":
            self.goal_manager.finish_step(step_id, output=output)
            for artifact in result.artifacts:
                self.goal_manager.append_artifact(goal_id, artifact)
        elif decision.decision == "retry":
            self.goal_manager.memory.update_goal_step(
                step_id,
                status="pending",
                output=output,
                error=decision.reason,
                retry_count=retry_count + 1,
                finished_at=time.time(),
            )
        elif decision.decision == "fail":
            self.goal_manager.fail_step(step_id, decision.reason, output=output)
        else:
            self.goal_manager.fail_step(step_id, decision.reason, output=output)

        log.info(
            "goal_step_reviewed",
            goal_id=goal_id,
            step_id=step_id,
            action=step.action,
            decision=decision.decision,
            elapsed_ms=result.elapsed_ms,
        )
        return decision

    async def _execute_controller_step(
        self,
        controller: GoalController,
        goal: dict,
        step: StepSpec,
    ) -> StepResult:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                controller.execute_step(goal, step),
                timeout=step.timeout or self.default_step_timeout,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return StepResult(
                ok=False,
                action=step.action,
                error=f"step timeout after {step.timeout or self.default_step_timeout}s",
                blocking=True,
                elapsed_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return StepResult(
                ok=False,
                action=step.action,
                error=f"{type(e).__name__}: {e}",
                blocking=True,
                elapsed_ms=elapsed_ms,
            )
        if result.elapsed_ms <= 0:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
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

    def _mark_step_for_retry(self, step_record: dict, decision: SupervisorDecision) -> None:
        retry_count = int(step_record.get("retry_count") or 0)
        self.goal_manager.memory.update_goal_step(
            step_record["step_id"],
            status="pending",
            retry_count=retry_count + 1,
            error=decision.reason,
        )

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
