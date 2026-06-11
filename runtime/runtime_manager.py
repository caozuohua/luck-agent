"""
runtime/runtime_manager.py - Goal Runtime entrypoint.

RuntimeManager routes messages to Skills, persists accepted Goals, and submits
their IDs to RuntimeTaskQueue. Background workers own execution.
"""
from __future__ import annotations

import asyncio
from typing import Any

from core.log import get_logger
from runtime.events import NoopRuntimeEventRecorder
from runtime.task_queue import RuntimeTaskQueue
from skills.base import SkillContext
from skills.registry import SkillNotFoundError, SkillRegistry
from skills.router import SkillRouter

log = get_logger()


class RuntimeManager:
    """Route messages into persistent Goal Runtime work."""

    def __init__(
        self,
        *,
        goal_manager,
        execution_engine=None,
        queue: RuntimeTaskQueue | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_router: SkillRouter | None = None,
        event_recorder=None,
        acceptance_timeout: float = 300.0,
    ) -> None:
        self.goal_manager = goal_manager
        self.execution_engine = execution_engine  # compatibility only
        self.queue = queue or RuntimeTaskQueue(max_active=1)
        self.event_recorder = event_recorder or NoopRuntimeEventRecorder()
        self.acceptance_timeout = acceptance_timeout
        self._acceptance_gates: dict[str, asyncio.Event] = {}

        if skill_registry is None and skill_router is None:
            raise ValueError("skill_registry or skill_router is required")
        if (
            skill_registry is not None
            and skill_router is not None
            and skill_router.registry is not skill_registry
        ):
            raise ValueError("skill_router must use the same registry")
        if skill_registry is None:
            assert skill_router is not None
            skill_registry = skill_router.registry
        self.skill_registry = skill_registry
        self.skill_router = skill_router or SkillRouter(self.skill_registry)

    async def handle_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
        model_override: str = "",
    ) -> dict[str, Any]:
        context = SkillContext(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            model_override=model_override,
        )
        route = self.skill_router.route(
            context,
            match_error_handler=self._record_route_error,
        )
        skill = route.skill
        metadata = skill.metadata

        if route.execution_mode == "legacy_inline":
            self._record(
                "route.fallback",
                skill=metadata.name,
                intent=route.intent,
                user_id=user_id,
                chat_id=chat_id,
                payload={"reason": route.reason},
            )
            return {
                "handled": False,
                "skill": metadata.name,
                "goal_id": "",
                "intent": route.intent,
                "reason": route.reason,
            }

        self._record(
            "route.matched",
            skill=metadata.name,
            intent=route.intent,
            user_id=user_id,
            chat_id=chat_id,
            payload={"reason": route.reason, "score": route.score},
        )
        request = skill.build_goal(context)
        plan = dict(request.plan)
        plan.update(
            {
                "source_message": text,
                "skill": metadata.name,
                "skill_version": metadata.version,
            }
        )
        goal_id = self.goal_manager.create_goal(
            user_id=user_id,
            chat_id=chat_id,
            title=request.title,
            intent=request.intent,
            success_criteria=list(request.success_criteria),
            plan=plan,
        )
        event_fields = {
            "goal_id": goal_id,
            "skill": metadata.name,
            "intent": request.intent,
            "user_id": user_id,
            "chat_id": chat_id,
        }
        self._record(
            "goal.created",
            **event_fields,
            status="pending",
            payload={"skill_version": metadata.version},
        )
        self._acceptance_gates[goal_id] = asyncio.Event()

        try:
            item = await self.queue.submit(
                goal_id=goal_id,
                user_id=user_id,
                chat_id=chat_id,
                priority=metadata.priority,
                meta={
                    "skill": metadata.name,
                    "intent": request.intent,
                    "reason": route.reason,
                    "skill_version": metadata.version,
                },
            )
        except Exception as error:
            self.goal_manager.fail_goal(
                goal_id,
                "queue submission failed",
            )
            self._record(
                "goal.failed",
                **event_fields,
                status="failed",
                payload={"error_type": type(error).__name__},
            )
            self._acceptance_gates.pop(goal_id, None)
            raise
        self._record(
            "goal.accepted",
            **event_fields,
            status="accepted",
            payload={"queue_status": item.status},
        )
        self._record(
            "queue.submitted",
            **event_fields,
            status=item.status,
            payload={"priority": metadata.priority},
        )
        log.info(
            "runtime_goal_accepted",
            goal_id=goal_id,
            skill=metadata.name,
            intent=request.intent,
            queue_status=item.status,
        )

        return {
            "handled": True,
            "skill": metadata.name,
            "goal_id": goal_id,
            "intent": request.intent,
            "status": "accepted",
            "queue_status": item.status,
            "summary": self.goal_manager.summary(goal_id),
        }

    async def queue_snapshot(self) -> dict:
        return await self.queue.snapshot()

    def mark_accepted(self, goal_id: str) -> None:
        gate = self._acceptance_gates.pop(goal_id, None)
        if gate is not None:
            gate.set()

    async def wait_until_accepted(
        self,
        goal_id: str,
        timeout: float | None = None,
    ) -> None:
        gate = self._acceptance_gates.get(goal_id)
        if gate is None:
            return
        wait_timeout = self.acceptance_timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(gate.wait(), timeout=wait_timeout)
        except TimeoutError:
            log.warning(
                "runtime_acceptance_wait_timeout",
                goal_id=goal_id,
                timeout=wait_timeout,
            )
        finally:
            if self._acceptance_gates.get(goal_id) is gate:
                self._acceptance_gates.pop(goal_id, None)

    async def recover_goals(self) -> int:
        goals = self.goal_manager.recover_interrupted_goals()
        submitted = 0
        for goal in goals:
            existing = await self.queue.get_item(goal["goal_id"])
            if (
                existing is not None
                and existing.status in {"pending", "running"}
            ):
                continue
            try:
                skill = self.skill_registry.resolve_goal(goal)
            except SkillNotFoundError:
                reason = "missing skill"
                self.goal_manager.block_goal(goal["goal_id"], reason)
                self._record(
                    "goal.blocked",
                    goal_id=goal["goal_id"],
                    intent=str(goal.get("intent") or "general"),
                    status="blocked",
                    user_id=str(goal.get("user_id") or ""),
                    chat_id=str(goal.get("chat_id") or ""),
                    payload={"error_type": "SkillNotFoundError"},
                )
                continue

            metadata = skill.metadata
            plan = goal.get("plan")
            persisted_version = (
                plan.get("skill_version")
                if isinstance(plan, dict)
                else None
            )
            skill_version = (
                persisted_version
                if isinstance(persisted_version, str) and persisted_version
                else metadata.version
            )
            intent = str(goal.get("intent") or metadata.intent or "general")
            event_fields = {
                "goal_id": goal["goal_id"],
                "skill": metadata.name,
                "intent": intent,
                "user_id": goal["user_id"],
                "chat_id": goal["chat_id"],
            }
            try:
                item = await self.queue.submit(
                    goal_id=goal["goal_id"],
                    user_id=goal["user_id"],
                    chat_id=goal["chat_id"],
                    priority=metadata.priority,
                    meta={
                        "skill": metadata.name,
                        "skill_version": skill_version,
                        "intent": intent,
                        "reason": "startup_recovery",
                    },
                )
            except Exception as error:
                self.goal_manager.block_goal(
                    goal["goal_id"],
                    "recovery queue submission failed",
                )
                self._record(
                    "goal.blocked",
                    **event_fields,
                    status="blocked",
                    payload={"error_type": type(error).__name__},
                )
                continue
            self._record(
                "queue.submitted",
                **event_fields,
                status=item.status,
                payload={
                    "priority": metadata.priority,
                    "reason": "startup_recovery",
                },
            )
            self._record(
                "goal.recovered",
                **event_fields,
                status=str(goal.get("status") or ""),
                payload={"skill_version": skill_version},
            )
            submitted += 1
        log.info("runtime_goals_recovered", count=submitted)
        return submitted

    async def cancel_goal(
        self,
        goal_id: str,
        reason: str = "user_cancelled",
    ) -> dict:
        self._release_acceptance_gate(goal_id)
        goal = self.goal_manager.get_goal(goal_id)
        status = goal.get("status")
        item = await self.queue.get_item(goal_id)

        if status in {"done", "failed", "cancelled"}:
            await self._align_queue_to_terminal_goal(goal, item)
            return goal

        if item is None:
            goal = self.goal_manager.cancel_goal(goal_id, reason)
            self._record(
                "goal.cancelled",
                **self._goal_event_fields(goal),
                status="cancelled",
                payload={"reason": str(goal.get("error") or reason)},
            )
            return goal

        if item.status in {
            "done",
            "failed",
            "cancelled",
            "interrupted",
        }:
            return self._align_goal_to_terminal_queue(goal_id, item, reason)

        persisted_goal: dict[str, Any] | None = None

        def persist_cancellation(_item: Any) -> None:
            nonlocal persisted_goal
            persisted_goal = self.goal_manager.cancel_goal(goal_id, reason)

        cancelled = await self.queue.cancel(
            goal_id,
            reason,
            before_transition=persist_cancellation,
        )
        if not cancelled:
            current_item = await self.queue.get_item(goal_id)
            if (
                current_item is not None
                and current_item.status in {
                    "done",
                    "failed",
                    "cancelled",
                    "interrupted",
                }
            ):
                return self._align_goal_to_terminal_queue(
                    goal_id,
                    current_item,
                    reason,
                )
            return self.goal_manager.get_goal(goal_id)

        assert persisted_goal is not None
        event_fields = self._goal_event_fields(persisted_goal)
        self._record(
            "queue.cancelled",
            **event_fields,
            status="cancelled",
            payload={"reason": reason},
        )
        self._record(
            "goal.cancelled",
            **event_fields,
            status="cancelled",
            payload={
                "reason": str(persisted_goal.get("error") or reason)
            },
        )
        return persisted_goal

    def _release_acceptance_gate(self, goal_id: str) -> None:
        gate = self._acceptance_gates.pop(goal_id, None)
        if gate is not None:
            gate.set()

    async def _align_queue_to_terminal_goal(
        self,
        goal: dict[str, Any],
        item: Any,
    ) -> None:
        if item is None:
            return
        goal_id = str(goal["goal_id"])
        status = goal.get("status")
        if status == "done":
            await self.queue.mark_done(goal_id)
        elif status == "failed":
            await self.queue.mark_failed(
                goal_id,
                str(goal.get("error") or "goal failed"),
            )
        elif status == "cancelled":
            await self.queue.mark_cancelled(
                goal_id,
                str(goal.get("error") or "goal cancelled"),
            )

    def _align_goal_to_terminal_queue(
        self,
        goal_id: str,
        item: Any,
        cancel_reason: str,
    ) -> dict[str, Any]:
        error = str(getattr(item, "error", "") or "")
        if item.status == "done":
            return self.goal_manager.complete_goal(goal_id)
        if item.status == "failed":
            return self.goal_manager.fail_goal(
                goal_id,
                error or "queue failed",
            )
        if item.status == "cancelled":
            return self.goal_manager.cancel_goal(
                goal_id,
                error or cancel_reason,
            )
        return self.goal_manager.pause_goal(
            goal_id,
            error or "queue interrupted",
        )

    def _record_route_error(
        self,
        skill,
        context: SkillContext,
        error: Exception,
    ) -> None:
        self._record(
            "route.error",
            skill=skill.metadata.name,
            intent=skill.metadata.intent,
            user_id=context.user_id,
            chat_id=context.chat_id,
            payload={
                "error_type": type(error).__name__,
                "context": {
                    "user_id": context.user_id,
                    "chat_id": context.chat_id,
                    "message_id": context.message_id,
                },
            },
        )

    def _record(self, event_type: str, **kwargs: Any) -> None:
        try:
            self.event_recorder.record(event_type, **kwargs)
        except Exception as error:
            log.warning(
                "runtime_event_record_failed",
                event_type=event_type,
                error=type(error).__name__,
            )

    @staticmethod
    def _goal_event_fields(goal: dict[str, Any]) -> dict[str, str]:
        plan = goal.get("plan")
        skill = plan.get("skill", "") if isinstance(plan, dict) else ""
        return {
            "goal_id": str(goal.get("goal_id") or ""),
            "skill": str(skill or ""),
            "intent": str(goal.get("intent") or "general"),
            "user_id": str(goal.get("user_id") or ""),
            "chat_id": str(goal.get("chat_id") or ""),
        }
