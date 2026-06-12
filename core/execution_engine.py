"""
core/execution_engine.py — Luck-Agent 2.0 standardized execution engine.

The engine is generic:
- GoalManager owns lifecycle and persistence.
- ExecutionEngine owns loop control and dispatch.
- Supervisor owns verification, retry/block decisions, and lesson capture.
- Goal Skills own actual business steps.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from core.goal import EXECUTION_TERMINAL_STATUSES
from core.log import get_logger
from core.protocols import ToolResult
from core.supervisor import Supervisor, SupervisorDecision
from runtime.events import NoopRuntimeEventRecorder
from skills.base import (
    BlockingSkillError,
    FatalSkillError,
    GoalSkill,
    RetryableSkillError,
    SkillExecutionError,
)
from skills.registry import SkillNotFoundError, SkillRegistry

log = get_logger()


@dataclass
class StepSpec:
    """Declarative step definition produced by a Goal Skill.

    External-effect Skills must pass idempotency_key to downstream APIs.
    Set replay_safe only when repeating an interrupted call is harmless.
    """

    name: str
    action: str
    input: dict[str, Any] = field(default_factory=dict)
    required: bool = True
    max_retry: int = 1
    timeout: int = 120
    replay_safe: bool = False
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StepResult:
    """Normalized execution result returned by Goal Skills."""

    ok: bool
    action: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    hint: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    next_step: str = ""
    blocking: bool = False
    elapsed_ms: int = 0

    def to_tool_result(self, tool: str = "skill") -> dict[str, Any]:
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


class ExecutionEngineError(RuntimeError):
    """Execution engine error."""


@dataclass(frozen=True)
class _FatalSkillOutcome:
    reason: str
    error_type: str
    elapsed_ms: int


class ExecutionEngine:
    """Generic execution loop for persisted goals."""

    def __init__(
        self,
        *,
        goal_manager,
        skill_registry: SkillRegistry,
        supervisor: Supervisor | None = None,
        event_recorder=None,
        max_steps: int = 30,
        default_step_timeout: int = 120,
    ) -> None:
        self.goal_manager = goal_manager
        self.skill_registry = skill_registry
        self.supervisor = supervisor or Supervisor(memory=getattr(goal_manager, "memory", None))
        self.event_recorder = (
            event_recorder
            if event_recorder is not None
            else NoopRuntimeEventRecorder()
        )
        self.max_steps = max_steps
        self.default_step_timeout = default_step_timeout

    async def run_goal(self, goal_id: str) -> dict:
        """Run a goal until done, blocked, failed, cancelled, or step budget exhausted."""
        goal = self.goal_manager.get_goal(goal_id)
        if self._is_terminal(goal):
            return goal

        goal, claimed = self.goal_manager.claim_goal(goal_id)
        if not claimed:
            return goal

        try:
            skill = self.skill_registry.resolve_goal(goal)
        except SkillNotFoundError:
            goal, changed = self._finish_goal(goal_id, "blocked", "missing skill")
            if not changed:
                return goal
            self._record(
                "goal.blocked",
                goal=goal,
                status="blocked",
                payload={"error_type": "SkillNotFoundError"},
            )
            return goal

        self._record("goal.started", goal=goal, skill=skill, status="running")
        try:
            has_plan = await self._ensure_plan(goal, skill)
        except Exception as error:
            error_type = type(error).__name__
            goal, changed = self._finish_goal(
                goal_id,
                "failed",
                f"skill build failed: {error_type}",
            )
            if not changed:
                return goal
            self._record(
                "goal.failed",
                goal=goal,
                skill=skill,
                status="failed",
                payload={"error_type": error_type},
            )
            return goal
        if has_plan is None:
            return self.goal_manager.get_goal(goal_id)
        if not has_plan:
            goal, changed = self._finish_goal(goal_id, "blocked", "empty plan")
            if not changed:
                return goal
            self._record(
                "goal.blocked",
                goal=goal,
                skill=skill,
                status="blocked",
                payload={"reason": "empty plan"},
            )
            return goal

        for _ in range(self.max_steps):
            goal = self.goal_manager.get_goal(goal_id)
            if goal.get("status") in {"done", "failed", "cancelled", "blocked"}:
                return goal

            steps = self.goal_manager.get_steps(goal_id)
            try:
                complete = await skill.is_goal_complete(goal, steps)
            except Exception as error:
                error_type = type(error).__name__
                goal, changed = self._finish_goal(
                    goal_id,
                    "failed",
                    f"skill completion failed: {error_type}",
                )
                if changed:
                    self._record(
                        "goal.failed",
                        goal=goal,
                        skill=skill,
                        status="failed",
                        payload={"error_type": error_type},
                    )
                return goal
            try:
                completion = self.supervisor.review_goal_completion(
                    goal=goal,
                    steps=steps,
                    complete=complete,
                )
            except Exception as error:
                error_type = type(error).__name__
                goal, changed = self._finish_goal(
                    goal_id,
                    "failed",
                    f"supervisor completion failed: {error_type}",
                )
                if changed:
                    self._record(
                        "goal.failed",
                        goal=goal,
                        skill=skill,
                        status="failed",
                        payload={"error_type": error_type},
                    )
                return goal
            if completion.get("is_goal_complete"):
                goal, changed = self._finish_goal(goal_id, "done")
                if not changed:
                    return goal
                self._record(
                    "goal.completed",
                    goal=goal,
                    skill=skill,
                    status="done",
                )
                return goal

            step_record = self._next_pending_step(steps)
            if not step_record:
                goal, changed = self._finish_goal(
                    goal_id,
                    "blocked",
                    "no pending step but goal is incomplete",
                )
                if not changed:
                    return goal
                self._record(
                    "goal.blocked",
                    goal=goal,
                    skill=skill,
                    status="blocked",
                    payload={"reason": "no pending step"},
                )
                return goal

            try:
                decision = await self.run_step(goal_id, step_record["step_id"])
            except Exception as error:
                error_type = type(error).__name__
                goal, changed = self._finish_goal(
                    goal_id,
                    "failed",
                    f"step persistence failed: {error_type}",
                )
                if changed:
                    self._record(
                        "goal.failed",
                        goal=goal,
                        skill=skill,
                        status="failed",
                        payload={"error_type": error_type},
                    )
                return goal

            goal = self.goal_manager.get_goal(goal_id)
            if self._is_terminal(goal):
                return goal
            if decision.decision == "pass":
                continue
            if decision.decision == "retry":
                continue
            if decision.decision == "fail":
                goal, changed = self._finish_goal(
                    goal_id,
                    "failed",
                    decision.reason or "step failed",
                )
                if not changed:
                    return goal
                self._record(
                    "goal.failed",
                    goal=goal,
                    skill=skill,
                    status="failed",
                    payload={"reason": decision.reason or "step failed"},
                )
                return goal
            goal, changed = self._finish_goal(
                goal_id,
                "blocked",
                decision.reason or "step blocked",
                step_record.get("name"),
            )
            if not changed:
                return goal
            self._record(
                "goal.blocked",
                goal=goal,
                skill=skill,
                status="blocked",
                payload={"reason": decision.reason or "step blocked"},
            )
            return goal

        goal, changed = self._finish_goal(
            goal_id,
            "blocked",
            f"max step budget exceeded: {self.max_steps}",
        )
        if not changed:
            return goal
        self._record(
            "goal.blocked",
            goal=goal,
            skill=skill,
            status="blocked",
            payload={"reason": "max steps", "max_steps": self.max_steps},
        )
        return goal

    async def run_step(self, goal_id: str, step_id: str) -> SupervisorDecision:
        """Run one persisted step and let Supervisor decide pass/retry/block/fail."""
        goal = self.goal_manager.get_goal(goal_id)
        step_record = self.goal_manager.memory.get_goal_step(step_id)
        if not step_record:
            raise ExecutionEngineError(f"step not found: {step_id}")
        skill = self.skill_registry.resolve_goal(goal)
        step = self._step_spec_from_record(step_record)

        started_step, claimed = self.goal_manager.start_step(step_id)
        if not claimed:
            return self._aborted_step_decision(
                self.goal_manager.get_goal(goal_id)
            )
        self._record(
            "step.started",
            goal=goal,
            skill=skill,
            step_id=step_id,
            status="running",
            payload={"name": started_step.get("name", ""), "action": step.action},
        )
        outcome = await self._execute_skill_step(skill, goal, step)

        current_goal = self.goal_manager.get_goal(goal_id)
        if self._is_terminal(current_goal):
            return self._aborted_step_decision(current_goal)

        if isinstance(outcome, _FatalSkillOutcome):
            committed = await self.goal_manager.commit_step_result(
                step_id,
                status="failed",
                output={
                    "error_type": outcome.error_type,
                    "error_class": "fatal",
                },
                error=outcome.reason,
            )
            if not committed:
                return self._aborted_step_decision(
                    self.goal_manager.get_goal(goal_id)
                )
            self._record(
                "step.failed",
                goal=goal,
                skill=skill,
                step_id=step_id,
                status="failed",
                payload={
                    "error_type": outcome.error_type,
                    "error_class": "fatal",
                    "action": step.action,
                },
            )
            return SupervisorDecision(
                decision="fail",
                verification={},
                reason=outcome.reason,
            )

        result = outcome
        retry_count = int(step_record.get("retry_count") or 0)
        max_retry = int((step_record.get("input") or {}).get("max_retry") or step.max_retry)
        try:
            decision = self.supervisor.review_step_result(
                goal=goal,
                step=step_record,
                result=result,
                retry_count=retry_count,
                max_retry=max_retry,
            )
        except Exception as error:
            error_type = type(error).__name__
            reason = f"supervisor step failed: {error_type}"
            committed = await self.goal_manager.commit_step_result(
                step_id,
                status="failed",
                output={"result": result.to_dict()},
                error=reason,
            )
            if not committed:
                return self._aborted_step_decision(
                    self.goal_manager.get_goal(goal_id)
                )
            self._record(
                "step.failed",
                goal=goal,
                skill=skill,
                step_id=step_id,
                status="failed",
                payload={"error_type": error_type, "action": step.action},
            )
            return SupervisorDecision(
                decision="fail",
                verification={},
                reason=reason,
            )
        self._record(
            "supervisor.decision",
            goal=goal,
            skill=skill,
            step_id=step_id,
            status=decision.decision,
            payload={
                "decision": decision.decision,
                "reason": decision.reason,
                "retry_count": retry_count,
                "max_retry": max_retry,
            },
        )

        output = {
            "result": result.to_dict(),
            "supervisor": decision.to_dict(),
        }
        if decision.decision == "pass":
            committed = await self.goal_manager.commit_step_result(
                step_id,
                status="done",
                output=output,
                artifacts=result.artifacts,
            )
            if not committed:
                return self._aborted_step_decision(
                    self.goal_manager.get_goal(goal_id)
                )
            self._record(
                "step.completed",
                goal=goal,
                skill=skill,
                step_id=step_id,
                status="done",
                payload={"action": step.action},
            )
        elif decision.decision == "retry":
            next_retry_count = retry_count + 1
            committed = await self.goal_manager.commit_step_result(
                step_id,
                status="pending",
                output=output,
                error=decision.reason,
                retry_increment=True,
            )
            if not committed:
                return self._aborted_step_decision(
                    self.goal_manager.get_goal(goal_id)
                )
            self._record(
                "step.retry",
                goal=goal,
                skill=skill,
                step_id=step_id,
                status="pending",
                payload={
                    "reason": decision.reason,
                    "retry_count": next_retry_count,
                    "max_retry": max_retry,
                },
            )
        elif decision.decision == "fail":
            committed = await self.goal_manager.commit_step_result(
                step_id,
                status="failed",
                output=output,
                error=decision.reason,
            )
            if not committed:
                return self._aborted_step_decision(
                    self.goal_manager.get_goal(goal_id)
                )
            self._record(
                "step.failed",
                goal=goal,
                skill=skill,
                step_id=step_id,
                status="failed",
                payload={"reason": decision.reason, "action": step.action},
            )
        else:
            committed = await self.goal_manager.commit_step_result(
                step_id,
                status="blocked",
                output=output,
                error=decision.reason,
            )
            if not committed:
                return self._aborted_step_decision(
                    self.goal_manager.get_goal(goal_id)
                )
            self._record(
                "step.blocked",
                goal=goal,
                skill=skill,
                step_id=step_id,
                status="blocked",
                payload={"reason": decision.reason, "action": step.action},
            )

        log.info(
            "goal_step_reviewed",
            goal_id=goal_id,
            step_id=step_id,
            action=step.action,
            decision=decision.decision,
            elapsed_ms=result.elapsed_ms,
        )
        return decision

    async def _execute_skill_step(
        self,
        skill: GoalSkill,
        goal: dict,
        step: StepSpec,
    ) -> StepResult | _FatalSkillOutcome:
        started = time.monotonic()
        timeout = step.timeout or self.default_step_timeout
        execution_task = asyncio.create_task(skill.execute_step(goal, step))
        try:
            done, _ = await asyncio.wait(
                {execution_task},
                timeout=timeout,
            )
        except asyncio.CancelledError:
            execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)
            raise

        if execution_task not in done:
            execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return StepResult(
                ok=False,
                action=step.action,
                error=f"step timeout after {timeout}s",
                blocking=False,
                elapsed_ms=elapsed_ms,
            )

        try:
            result = execution_task.result()
            if result.elapsed_ms <= 0:
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
            return result
        except RetryableSkillError as error:
            return StepResult(
                ok=False,
                action=step.action,
                error=str(error),
                hint=error.hint,
                blocking=False,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except BlockingSkillError as error:
            return StepResult(
                ok=False,
                action=step.action,
                error=str(error),
                hint=error.hint,
                blocking=True,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except (FatalSkillError, SkillExecutionError) as error:
            return _FatalSkillOutcome(
                reason=str(error),
                error_type=type(error).__name__,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as error:
            error_type = type(error).__name__
            return _FatalSkillOutcome(
                reason=f"skill execute failed: {error_type}",
                error_type=error_type,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

    async def _ensure_plan(self, goal: dict, skill: GoalSkill) -> bool | None:
        """Create persisted goal steps if the goal has no step records yet."""
        goal_id = goal["goal_id"]
        existing_steps = self.goal_manager.get_steps(goal_id)
        if existing_steps:
            return True
        plan = await skill.build_plan(goal)
        if not plan:
            return False
        created_steps, committed = self.goal_manager.commit_plan(goal_id, plan)
        if not committed:
            current_goal = self.goal_manager.get_goal(goal_id)
            if self._is_terminal(current_goal):
                return None
            return bool(self.goal_manager.get_steps(goal_id))

        for step, created in zip(plan, created_steps):
            self._record(
                "step.created",
                goal=goal,
                skill=skill,
                step_id=created["step_id"],
                status="pending",
                payload={"name": step.name, "action": step.action},
            )
        return True

    def _record(
        self,
        event_type: str,
        *,
        goal: dict,
        skill: GoalSkill | None = None,
        step_id: str = "",
        status: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        metadata = getattr(skill, "metadata", None)
        event_payload = dict(payload or {})
        event_payload["skill_version"] = (
            str(getattr(metadata, "version", "") or "")
        )
        try:
            self.event_recorder.record(
                event_type,
                goal_id=str(goal.get("goal_id") or ""),
                step_id=step_id,
                skill=str(getattr(metadata, "name", "") or ""),
                intent=str(goal.get("intent") or "general"),
                status=status,
                user_id=str(goal.get("user_id") or ""),
                chat_id=str(goal.get("chat_id") or ""),
                payload=event_payload,
            )
        except Exception as error:
            log.warning(
                "runtime_event_record_failed",
                event_type=event_type,
                goal_id=goal.get("goal_id", ""),
                error_type=type(error).__name__,
            )

    def _finish_goal(
        self,
        goal_id: str,
        status: str,
        error: str = "",
        current_step: str | None = None,
    ) -> tuple[dict, bool]:
        goal = self.goal_manager.get_goal(goal_id)
        if self._is_terminal(goal):
            return goal, False

        updates: dict[str, Any] = {"status": status, "error": error}
        if current_step is not None:
            updates["current_step"] = current_step
        updated = self.goal_manager.memory.update_goal_if_status(
            goal_id,
            {"running"},
            **updates,
        )
        return self.goal_manager.get_goal(goal_id), updated

    @staticmethod
    def _is_terminal(goal: dict) -> bool:
        return goal.get("status") in EXECUTION_TERMINAL_STATUSES

    @staticmethod
    def _aborted_step_decision(goal: dict) -> SupervisorDecision:
        return SupervisorDecision(
            decision="block",
            verification={},
            reason=f"goal {goal.get('status') or 'not running'}",
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
            replay_safe=bool(raw.get("replay_safe", False)),
            idempotency_key=str(
                raw.get("idempotency_key") or step_record.get("step_id") or ""
            ),
        )
