from __future__ import annotations

import asyncio
import unittest
import gc
import weakref
from dataclasses import dataclass
from typing import Any

from runtime.contracts import RuntimeHandleResult
from runtime.events import RuntimeEventRecorder
from runtime.runtime_manager import RuntimeManager
from runtime.task_queue import RuntimeTaskQueue
from skills.base import GoalRequest, SkillContext, SkillMatch, SkillMetadata
from skills.legacy_react import LegacyReactSkill
from skills.registry import SkillRegistry
from skills.router import SkillRouter


class FakeEventRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event_type: str, **kwargs: Any) -> None:
        self.events.append({"event_type": event_type, **kwargs})


class FakeRuntimeEventMemory:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append_runtime_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class FakeGoalManager:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.recoverable: list[dict[str, Any]] = []
        self.blocked: list[tuple[str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self.paused: list[tuple[str, str]] = []
        self.goals: dict[str, dict[str, Any]] = {}

    def create_goal(self, **kwargs: Any) -> str:
        self.created.append(kwargs)
        goal_id = f"goal-{len(self.created)}"
        self.goals[goal_id] = {
            "goal_id": goal_id,
            "status": "pending",
            "error": "",
            **kwargs,
        }
        return goal_id

    def summary(self, goal_id: str) -> str:
        return f"summary:{goal_id}"

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        return dict(self.goals[goal_id])

    def recover_interrupted_goals(self) -> list[dict[str, Any]]:
        return self.recoverable

    def block_goal(self, goal_id: str, reason: str) -> dict[str, Any]:
        self.blocked.append((goal_id, reason))
        goal = self.goals.setdefault(goal_id, {"goal_id": goal_id})
        goal.update(status="blocked", error=reason)
        return dict(goal)

    def fail_goal(self, goal_id: str, reason: str) -> dict[str, Any]:
        self.failed.append((goal_id, reason))
        goal = self.goals.setdefault(goal_id, {"goal_id": goal_id})
        goal.update(status="failed", error=reason)
        return dict(goal)

    def cancel_goal(self, goal_id: str, reason: str) -> dict[str, Any]:
        self.cancelled.append((goal_id, reason))
        goal = self.goals.setdefault(
            goal_id,
            {
                "goal_id": goal_id,
                "user_id": "cancel-user",
                "chat_id": "cancel-chat",
                "intent": "test_intent",
                "plan": {"skill": "test_skill"},
            },
        )
        if goal.get("status") not in {"done", "failed", "cancelled"}:
            goal.update(status="cancelled", error=reason)
        return dict(goal)

    def complete_goal(self, goal_id: str) -> dict[str, Any]:
        self.completed.append(goal_id)
        goal = self.goals[goal_id]
        goal.update(status="done", error="")
        return dict(goal)

    def pause_goal(self, goal_id: str, reason: str) -> dict[str, Any]:
        self.paused.append((goal_id, reason))
        goal = self.goals[goal_id]
        goal.update(status="interrupted", error=reason)
        return dict(goal)


class FailingOnceCancelGoalManager(FakeGoalManager):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_attempts = 0

    def cancel_goal(self, goal_id: str, reason: str) -> dict[str, Any]:
        self.cancel_attempts += 1
        if self.cancel_attempts == 1:
            raise RuntimeError("persistence failed")
        return super().cancel_goal(goal_id, reason)


@dataclass
class FakeQueueItem:
    status: str = "pending"


class RecordingQueue:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.cancellations: list[tuple[str, str]] = []
        self.items: dict[str, FakeQueueItem] = {}
        self.submit_errors: dict[str, Exception] = {}
        self.cancel_error: Exception | None = None

    async def submit(self, **kwargs: Any) -> FakeQueueItem:
        self.submissions.append(kwargs)
        goal_id = kwargs["goal_id"]
        if goal_id in self.submit_errors:
            raise self.submit_errors[goal_id]
        item = FakeQueueItem()
        self.items[goal_id] = item
        return item

    async def get_item(self, goal_id: str) -> FakeQueueItem | None:
        return self.items.get(goal_id)

    async def cancel(
        self,
        goal_id: str,
        reason: str,
        before_transition=None,
    ) -> bool:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancellations.append((goal_id, reason))
        item = self.items.get(goal_id)
        if item is None or item.status not in {"pending", "running"}:
            return False
        if before_transition is not None:
            before_transition(item)
        item.status = "cancelled"
        return True

    async def mark_done(self, goal_id: str) -> bool:
        self.items[goal_id].status = "done"
        return True

    async def mark_failed(self, goal_id: str, error: str) -> bool:
        self.items[goal_id].status = "failed"
        return True

    async def mark_cancelled(self, goal_id: str, reason: str) -> bool:
        self.items[goal_id].status = "cancelled"
        return True


class RacingCancelQueue(RecordingQueue):
    async def cancel(
        self,
        goal_id: str,
        reason: str,
        before_transition=None,
    ) -> bool:
        self.items[goal_id].status = "done"
        return False


class FakeGoalSkill:
    def __init__(
        self,
        *,
        name: str = "test_skill",
        intent: str = "test_intent",
        version: str = "2.3.4",
        priority: int = 23,
        matched: bool = True,
        raises: bool = False,
    ) -> None:
        self.metadata = SkillMetadata(
            name=name,
            version=version,
            intent=intent,
            description="runtime manager test skill",
            execution_mode="goal_runtime",
            priority=priority,
        )
        self.matched = matched
        self.raises = raises
        self.matched_context: SkillContext | None = None
        self.goal_context: SkillContext | None = None

    def match(self, context: SkillContext) -> SkillMatch:
        self.matched_context = context
        if self.raises:
            raise RuntimeError("broken matcher")
        return SkillMatch(
            matched=self.matched,
            score=0.9 if self.matched else 0.0,
            reason="test match" if self.matched else "no match",
        )

    def build_goal(self, context: SkillContext) -> GoalRequest:
        self.goal_context = context
        return GoalRequest(
            title="Built title",
            intent=self.metadata.intent,
            success_criteria=("criterion one", "criterion two"),
            plan={
                "skill": "untrusted",
                "skill_version": "0",
                "source_message": "normalized or replaced",
                "custom": "preserved",
            },
        )

    async def build_plan(self, goal: dict[str, Any]) -> list[Any]:
        return []

    async def execute_step(
        self,
        goal: dict[str, Any],
        step: Any,
    ) -> Any:
        raise AssertionError("RuntimeManager must not execute skill steps inline")

    async def is_goal_complete(
        self,
        goal: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> bool:
        return False


class FailingExecutionEngine:
    async def run_goal(self, goal_id: str) -> None:
        raise AssertionError("RuntimeManager must not execute goals inline")


class RuntimeSkillRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _manager(
        self,
        *skills: FakeGoalSkill,
        goal_manager: FakeGoalManager | None = None,
        queue: RecordingQueue | RuntimeTaskQueue | None = None,
        recorder: FakeEventRecorder | None = None,
        router: SkillRouter | None = None,
    ) -> tuple[RuntimeManager, FakeGoalManager, Any, FakeEventRecorder]:
        manager_goals = goal_manager or FakeGoalManager()
        manager_queue = queue or RecordingQueue()
        manager_recorder = recorder or FakeEventRecorder()
        registry = SkillRegistry([*skills, LegacyReactSkill()])
        manager = RuntimeManager(
            goal_manager=manager_goals,
            execution_engine=FailingExecutionEngine(),
            queue=manager_queue,
            skill_registry=registry,
            skill_router=router,
            event_recorder=manager_recorder,
        )
        return manager, manager_goals, manager_queue, manager_recorder

    def test_manager_requires_registry_or_router(self) -> None:
        with self.assertRaisesRegex(ValueError, "skill_registry or skill_router"):
            RuntimeManager(goal_manager=FakeGoalManager())

    def test_manager_rejects_mismatched_registry_and_router(self) -> None:
        registry = SkillRegistry([LegacyReactSkill()])
        other_registry = SkillRegistry([LegacyReactSkill()])

        with self.assertRaisesRegex(ValueError, "same registry"):
            RuntimeManager(
                goal_manager=FakeGoalManager(),
                skill_registry=registry,
                skill_router=SkillRouter(other_registry),
            )

    def test_manager_derives_or_constructs_matching_skill_dependency(self) -> None:
        registry = SkillRegistry([LegacyReactSkill()])
        from_registry = RuntimeManager(
            goal_manager=FakeGoalManager(),
            skill_registry=registry,
        )
        router = SkillRouter(registry)
        from_router = RuntimeManager(
            goal_manager=FakeGoalManager(),
            skill_router=router,
        )

        self.assertIs(from_registry.skill_router.registry, registry)
        self.assertIs(from_router.skill_registry, registry)

    async def test_goal_route_builds_authoritative_goal_and_result_contract(self) -> None:
        skill = FakeGoalSkill()
        manager, goals, queue, recorder = self._manager(skill)

        result = await manager.handle_message(
            user_id="user-1",
            chat_id="chat-1",
            text="  ORIGINAL Request  ",
            message_id="message-1",
            model_override="model-pro",
        )

        self.assertIsInstance(result, RuntimeHandleResult)
        self.assertEqual(
            result,
            {
                "handled": True,
                "skill": "test_skill",
                "goal_id": "goal-1",
                "intent": "test_intent",
                "status": "accepted",
                "queue_status": "pending",
                "summary": "summary:goal-1",
                "reason": "test match",
            },
        )
        self.assertEqual(
            goals.created,
            [{
                "user_id": "user-1",
                "chat_id": "chat-1",
                "title": "Built title",
                "intent": "test_intent",
                "success_criteria": ["criterion one", "criterion two"],
                "plan": {
                    "skill": "test_skill",
                    "skill_version": "2.3.4",
                    "source_message": "  ORIGINAL Request  ",
                    "custom": "preserved",
                },
            }],
        )
        self.assertEqual(skill.matched_context.text, "original request")
        self.assertEqual(skill.goal_context.text, "  ORIGINAL Request  ")
        self.assertEqual(skill.goal_context.message_id, "message-1")
        self.assertEqual(skill.goal_context.model_override, "model-pro")
        self.assertEqual(queue.submissions[0]["priority"], 23)
        self.assertEqual(
            queue.submissions[0]["meta"],
            {
                "skill": "test_skill",
                "intent": "test_intent",
                "reason": "test match",
                "skill_version": "2.3.4",
            },
        )
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            [
                "route.matched",
                "goal.created",
                "goal.accepted",
                "queue.submitted",
            ],
        )

    async def test_goal_acceptance_gate_waits_until_marked_then_cleans_up(
        self,
    ) -> None:
        manager, _, _, _ = self._manager(FakeGoalSkill())
        result = await manager.handle_message(
            user_id="user",
            chat_id="chat",
            text="request",
        )

        waiter = asyncio.create_task(
            manager.wait_until_accepted(result["goal_id"], timeout=1)
        )
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        manager.mark_accepted(result["goal_id"])
        await waiter

        self.assertNotIn(result["goal_id"], manager._acceptance_gates)

    async def test_legacy_and_recovery_goals_have_no_acceptance_gate(
        self,
    ) -> None:
        manager, goals, _, _ = self._manager(FakeGoalSkill(matched=False))
        goals.recoverable = []

        result = await manager.handle_message(
            user_id="user",
            chat_id="chat",
            text="ordinary chat",
        )

        self.assertFalse(result["handled"])
        await asyncio.wait_for(
            manager.wait_until_accepted("legacy-or-recovered", timeout=0.01),
            timeout=0.1,
        )

    async def test_legacy_fallback_creates_no_goal_and_submits_nothing(self) -> None:
        skill = FakeGoalSkill(matched=False)
        manager, goals, queue, recorder = self._manager(skill)

        result = await manager.handle_message(
            user_id="user",
            chat_id="chat",
            text="ordinary chat",
        )

        self.assertIsInstance(result, RuntimeHandleResult)
        self.assertEqual(
            result,
            {
                "handled": False,
                "skill": "legacy_react",
                "goal_id": "",
                "intent": "general",
                "status": "fallback",
                "queue_status": "",
                "summary": "",
                "reason": "legacy fallback",
            },
        )
        self.assertEqual(goals.created, [])
        self.assertEqual(queue.submissions, [])
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["route.fallback"],
        )

    async def test_default_router_records_match_error_then_falls_back(self) -> None:
        broken = FakeGoalSkill(name="broken", raises=True)
        manager, _, _, recorder = self._manager(broken)

        result = await manager.handle_message(
            user_id="user",
            chat_id="chat",
            text="request",
            message_id="message",
        )

        self.assertFalse(result["handled"])
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["route.error", "route.fallback"],
        )
        self.assertEqual(recorder.events[0]["skill"], "broken")
        self.assertEqual(recorder.events[0]["user_id"], "user")
        self.assertEqual(recorder.events[0]["chat_id"], "chat")
        self.assertEqual(
            recorder.events[0]["payload"],
            {
                "error_type": "RuntimeError",
                "context": {
                    "user_id": "user",
                    "chat_id": "chat",
                    "message_id": "message",
                },
            },
        )

    async def test_route_error_is_accepted_by_runtime_event_recorder(self) -> None:
        broken = FakeGoalSkill(name="broken", raises=True)
        memory = FakeRuntimeEventMemory()
        manager, _, _, _ = self._manager(
            broken,
            recorder=RuntimeEventRecorder(memory),
        )

        await manager.handle_message(
            user_id="user",
            chat_id="chat",
            text="request",
            message_id="message",
        )

        route_error = next(
            event for event in memory.events
            if event["event_type"] == "route.error"
        )
        self.assertEqual(route_error["skill"], "broken")
        self.assertEqual(route_error["user_id"], "user")
        self.assertEqual(route_error["chat_id"], "chat")
        self.assertEqual(route_error["payload"]["error_type"], "RuntimeError")
        self.assertEqual(
            route_error["payload"]["context"]["message_id"],
            "message",
        )

    async def test_external_router_preserves_hook_and_records_match_error(self) -> None:
        broken = FakeGoalSkill(name="broken", raises=True)
        registry = SkillRegistry([broken, LegacyReactSkill()])
        errors: list[str] = []
        router = SkillRouter(
            registry,
            match_error_handler=lambda skill, context, error: errors.append(
                skill.metadata.name
            ),
        )
        recorder = FakeEventRecorder()
        manager = RuntimeManager(
            goal_manager=FakeGoalManager(),
            queue=RecordingQueue(),
            skill_registry=registry,
            skill_router=router,
            event_recorder=recorder,
        )

        await manager.handle_message(user_id="u", chat_id="c", text="request")

        self.assertEqual(errors, ["broken"])
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["route.error", "route.fallback"],
        )

    async def test_external_router_handler_failure_does_not_block_recording_or_fallback(
        self,
    ) -> None:
        broken = FakeGoalSkill(name="broken", raises=True)
        registry = SkillRegistry([broken, LegacyReactSkill()])
        calls: list[str] = []

        def failing_handler(skill, context, error) -> None:
            calls.append(skill.metadata.name)
            raise RuntimeError("handler secret")

        router = SkillRouter(registry, match_error_handler=failing_handler)
        recorder = FakeEventRecorder()
        manager = RuntimeManager(
            goal_manager=FakeGoalManager(),
            queue=RecordingQueue(),
            skill_router=router,
            event_recorder=recorder,
        )

        result = await manager.handle_message(
            user_id="u",
            chat_id="c",
            text="request",
            message_id="m",
        )

        self.assertFalse(result["handled"])
        self.assertEqual(calls, ["broken"])
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["route.error", "route.fallback"],
        )

    async def test_shared_router_does_not_retain_manager_or_duplicate_handlers(self) -> None:
        broken = FakeGoalSkill(name="broken", raises=True)
        registry = SkillRegistry([broken, LegacyReactSkill()])
        router = SkillRouter(registry)
        recorder = FakeEventRecorder()
        manager = RuntimeManager(
            goal_manager=FakeGoalManager(),
            queue=RecordingQueue(),
            skill_router=router,
            event_recorder=recorder,
        )
        manager_ref = weakref.ref(manager)

        del manager
        gc.collect()
        router.route(
            SkillContext(user_id="u", chat_id="c", text="request")
        )

        self.assertIsNone(manager_ref())
        self.assertEqual(recorder.events, [])

    async def test_shared_router_records_route_error_only_for_active_manager(self) -> None:
        broken = FakeGoalSkill(name="broken", raises=True)
        registry = SkillRegistry([broken, LegacyReactSkill()])
        router = SkillRouter(registry)
        first_recorder = FakeEventRecorder()
        second_recorder = FakeEventRecorder()
        first = RuntimeManager(
            goal_manager=FakeGoalManager(),
            queue=RecordingQueue(),
            skill_router=router,
            event_recorder=first_recorder,
        )
        second = RuntimeManager(
            goal_manager=FakeGoalManager(),
            queue=RecordingQueue(),
            skill_router=router,
            event_recorder=second_recorder,
        )

        await first.handle_message(user_id="u1", chat_id="c1", text="request")
        await second.handle_message(user_id="u2", chat_id="c2", text="request")

        self.assertEqual(
            [event["event_type"] for event in first_recorder.events],
            ["route.error", "route.fallback"],
        )
        self.assertEqual(
            [event["event_type"] for event in second_recorder.events],
            ["route.error", "route.fallback"],
        )
        self.assertEqual(first_recorder.events[0]["user_id"], "u1")
        self.assertEqual(second_recorder.events[0]["user_id"], "u2")

    async def test_queue_submission_failure_fails_goal_and_reraises(self) -> None:
        queue = RecordingQueue()
        queue.submit_errors["goal-1"] = RuntimeError("provider secret")
        manager, goals, _, recorder = self._manager(
            FakeGoalSkill(),
            queue=queue,
        )

        with self.assertRaisesRegex(RuntimeError, "provider secret"):
            await manager.handle_message(
                user_id="u",
                chat_id="c",
                text="request",
            )

        self.assertEqual(
            goals.failed,
            [("goal-1", "queue submission failed")],
        )
        self.assertEqual(goals.get_goal("goal-1")["status"], "failed")
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["route.matched", "goal.created", "goal.failed"],
        )
        self.assertEqual(
            recorder.events[-1]["payload"],
            {"error_type": "RuntimeError"},
        )
        self.assertNotIn("provider secret", repr(recorder.events))
        self.assertNotIn("goal-1", manager._acceptance_gates)

    async def test_cancel_releases_and_cleans_acceptance_gate(self) -> None:
        manager, _, _, _ = self._manager(FakeGoalSkill())
        result = await manager.handle_message(
            user_id="user",
            chat_id="chat",
            text="request",
        )
        waiter = asyncio.create_task(
            manager.wait_until_accepted(result["goal_id"], timeout=1)
        )
        await asyncio.sleep(0)

        await manager.cancel_goal(result["goal_id"], "user cancelled")
        await waiter

        self.assertNotIn(result["goal_id"], manager._acceptance_gates)

    async def test_recovery_uses_resolved_skill_priority_identity_and_intent_fallback(self) -> None:
        persisted = FakeGoalSkill(
            name="persisted",
            intent="persisted_intent",
            version="7.0",
            priority=7,
        )
        old = FakeGoalSkill(
            name="old",
            intent="old_intent",
            version="1.5",
            priority=15,
        )
        goals = FakeGoalManager()
        goals.recoverable = [
            {
                "goal_id": "goal-persisted",
                "user_id": "u1",
                "chat_id": "c1",
                "intent": "wrong_intent",
                "plan": {"skill": "persisted", "skill_version": "6.0"},
            },
            {
                "goal_id": "goal-old",
                "user_id": "u2",
                "chat_id": "c2",
                "intent": "old_intent",
                "plan": {},
            },
        ]
        manager, _, queue, recorder = self._manager(
            persisted,
            old,
            goal_manager=goals,
        )

        recovered = await manager.recover_goals()

        self.assertEqual(recovered, 2)
        self.assertEqual(
            [
                (
                    item["goal_id"],
                    item["priority"],
                    item["meta"],
                )
                for item in queue.submissions
            ],
            [
                (
                    "goal-persisted",
                    7,
                    {
                        "skill": "persisted",
                        "skill_version": "6.0",
                        "intent": "wrong_intent",
                        "reason": "startup_recovery",
                    },
                ),
                (
                    "goal-old",
                    15,
                    {
                        "skill": "old",
                        "skill_version": "1.5",
                        "intent": "old_intent",
                        "reason": "startup_recovery",
                    },
                ),
            ],
        )
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            [
                "queue.submitted",
                "goal.recovered",
                "queue.submitted",
                "goal.recovered",
            ],
        )

    async def test_recovery_skips_goal_already_pending_or_running_in_queue(self) -> None:
        skill = FakeGoalSkill()
        goals = FakeGoalManager()
        goals.recoverable = [
            {
                "goal_id": "goal-pending",
                "user_id": "u1",
                "chat_id": "c1",
                "intent": "test_intent",
                "plan": {"skill": "test_skill"},
            },
            {
                "goal_id": "goal-running",
                "user_id": "u2",
                "chat_id": "c2",
                "intent": "test_intent",
                "plan": {"skill": "test_skill"},
            },
        ]
        queue = RecordingQueue()
        queue.items = {
            "goal-pending": FakeQueueItem("pending"),
            "goal-running": FakeQueueItem("running"),
        }
        manager, _, _, recorder = self._manager(
            skill,
            goal_manager=goals,
            queue=queue,
        )

        recovered = await manager.recover_goals()

        self.assertEqual(recovered, 0)
        self.assertEqual(queue.submissions, [])
        self.assertEqual(recorder.events, [])

    async def test_recovery_submission_failure_blocks_goal_and_continues(self) -> None:
        skill = FakeGoalSkill()
        goals = FakeGoalManager()
        goals.recoverable = [
            {
                "goal_id": "goal-broken",
                "user_id": "u1",
                "chat_id": "c1",
                "intent": "test_intent",
                "plan": {"skill": "test_skill"},
            },
            {
                "goal_id": "goal-healthy",
                "user_id": "u2",
                "chat_id": "c2",
                "intent": "test_intent",
                "plan": {"skill": "test_skill"},
            },
        ]
        queue = RecordingQueue()
        queue.submit_errors["goal-broken"] = RuntimeError("recovery secret")
        manager, _, _, recorder = self._manager(
            skill,
            goal_manager=goals,
            queue=queue,
        )

        recovered = await manager.recover_goals()

        self.assertEqual(recovered, 1)
        self.assertEqual(
            goals.blocked,
            [("goal-broken", "recovery queue submission failed")],
        )
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["goal.blocked", "queue.submitted", "goal.recovered"],
        )
        self.assertEqual(
            recorder.events[0]["payload"],
            {"error_type": "RuntimeError"},
        )
        self.assertNotIn("recovery secret", repr(recorder.events))

    async def test_recovery_blocks_missing_skill_and_continues(self) -> None:
        healthy = FakeGoalSkill(name="healthy", intent="healthy_intent")
        goals = FakeGoalManager()
        goals.recoverable = [
            {
                "goal_id": "goal-missing",
                "user_id": "u1",
                "chat_id": "c1",
                "intent": "missing_intent",
                "plan": {"skill": "missing"},
            },
            {
                "goal_id": "goal-healthy",
                "user_id": "u2",
                "chat_id": "c2",
                "intent": "healthy_intent",
                "plan": {"skill": "healthy", "skill_version": "2.3.4"},
            },
        ]
        manager, _, queue, recorder = self._manager(
            healthy,
            goal_manager=goals,
        )

        recovered = await manager.recover_goals()

        self.assertEqual(recovered, 1)
        self.assertEqual(queue.submissions[0]["goal_id"], "goal-healthy")
        self.assertEqual(goals.blocked[0], ("goal-missing", "missing skill"))
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["goal.blocked", "queue.submitted", "goal.recovered"],
        )
        self.assertEqual(
            recorder.events[0]["payload"],
            {"error_type": "SkillNotFoundError"},
        )

    async def test_missing_skill_does_not_leak_corrupt_plan_value(self) -> None:
        goals = FakeGoalManager()
        secret = "secret-api-key"
        goals.recoverable = [
            {
                "goal_id": "goal-missing",
                "user_id": "u1",
                "chat_id": "c1",
                "intent": "missing_intent",
                "plan": {"skill": {"token": secret}},
            },
        ]
        manager, _, _, recorder = self._manager(goal_manager=goals)

        self.assertEqual(await manager.recover_goals(), 0)

        self.assertEqual(goals.blocked, [("goal-missing", "missing skill")])
        self.assertNotIn(secret, repr(recorder.events))

    async def test_cancel_active_queue_item_records_queue_then_goal(self) -> None:
        goals = FakeGoalManager()
        goals.goals["goal-cancel"] = {
            "goal_id": "goal-cancel",
            "status": "pending",
            "error": "",
            "user_id": "cancel-user",
            "chat_id": "cancel-chat",
            "intent": "test_intent",
            "plan": {"skill": "test_skill"},
        }
        queue = RecordingQueue()
        queue.items["goal-cancel"] = FakeQueueItem("pending")
        manager, _, _, recorder = self._manager(
            goal_manager=goals,
            queue=queue,
        )

        result = await manager.cancel_goal("goal-cancel", "user cancelled")

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(goals.cancelled, [("goal-cancel", "user cancelled")])
        self.assertEqual(
            queue.cancellations,
            [("goal-cancel", "user cancelled")],
        )
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["queue.cancelled", "goal.cancelled"],
        )

    async def test_cancel_queue_error_does_not_modify_active_goal(self) -> None:
        goals = FakeGoalManager()
        goals.goals["goal-cancel"] = {
            "goal_id": "goal-cancel",
            "status": "running",
            "error": "",
        }
        queue = RecordingQueue()
        queue.items["goal-cancel"] = FakeQueueItem("running")
        queue.cancel_error = RuntimeError("queue unavailable")
        manager, _, _, recorder = self._manager(
            goal_manager=goals,
            queue=queue,
        )

        with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
            await manager.cancel_goal("goal-cancel", "user cancelled")

        self.assertEqual(goals.get_goal("goal-cancel")["status"], "running")
        self.assertEqual(goals.cancelled, [])
        self.assertEqual(recorder.events, [])

    async def test_cancel_persistence_failure_keeps_goal_and_queue_active(
        self,
    ) -> None:
        goals = FailingOnceCancelGoalManager()
        goals.goals["goal-cancel"] = {
            "goal_id": "goal-cancel",
            "status": "pending",
            "error": "",
            "user_id": "cancel-user",
            "chat_id": "cancel-chat",
            "intent": "test_intent",
            "plan": {"skill": "test_skill"},
        }
        queue = RuntimeTaskQueue()
        await queue.submit(
            goal_id="goal-cancel",
            user_id="cancel-user",
            chat_id="cancel-chat",
        )
        manager, _, _, recorder = self._manager(
            goal_manager=goals,
            queue=queue,
        )

        with self.assertRaisesRegex(RuntimeError, "persistence failed"):
            await manager.cancel_goal("goal-cancel", "user cancelled")

        item = await queue.get_item("goal-cancel")
        self.assertEqual(goals.get_goal("goal-cancel")["status"], "pending")
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.error, "")
        self.assertIsNone(item.finished_at)
        self.assertEqual(recorder.events, [])

        result = await manager.cancel_goal(
            "goal-cancel",
            "user cancelled",
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(item.status, "cancelled")
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["queue.cancelled", "goal.cancelled"],
        )

    async def test_cancel_without_queue_item_cancels_goal_only(self) -> None:
        goals = FakeGoalManager()
        goals.goals["goal-cancel"] = {
            "goal_id": "goal-cancel",
            "status": "interrupted",
            "error": "",
        }
        manager, _, queue, recorder = self._manager(goal_manager=goals)

        result = await manager.cancel_goal("goal-cancel", "user cancelled")

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(queue.cancellations, [])
        self.assertEqual(
            [event["event_type"] for event in recorder.events],
            ["goal.cancelled"],
        )

    async def test_cancel_aligns_active_goal_to_terminal_queue_state(self) -> None:
        transitions = {
            "done": ("done", ""),
            "failed": ("failed", "worker failed"),
            "cancelled": ("cancelled", "worker cancelled"),
            "interrupted": ("interrupted", "worker interrupted"),
        }
        for queue_status, (goal_status, error) in transitions.items():
            with self.subTest(queue_status=queue_status):
                goals = FakeGoalManager()
                goals.goals["goal-race"] = {
                    "goal_id": "goal-race",
                    "status": "running",
                    "error": "",
                }
                queue = RecordingQueue()
                item = FakeQueueItem(queue_status)
                item.error = error
                queue.items["goal-race"] = item
                manager, _, _, _ = self._manager(
                    goal_manager=goals,
                    queue=queue,
                )

                result = await manager.cancel_goal(
                    "goal-race",
                    "late cancellation",
                )

                self.assertEqual(result["status"], goal_status)
                self.assertEqual(result["error"], error)
                self.assertEqual(queue.cancellations, [])

    async def test_cancel_rechecks_queue_after_losing_cancel_race(self) -> None:
        goals = FakeGoalManager()
        goals.goals["goal-race"] = {
            "goal_id": "goal-race",
            "status": "running",
            "error": "",
        }
        queue = RacingCancelQueue()
        queue.items["goal-race"] = FakeQueueItem("running")
        manager, _, _, _ = self._manager(
            goal_manager=goals,
            queue=queue,
        )

        result = await manager.cancel_goal("goal-race", "late cancellation")

        self.assertEqual(result["status"], "done")
        self.assertEqual(goals.completed, ["goal-race"])
        self.assertEqual(goals.cancelled, [])

    async def test_cancel_aligns_active_queue_to_terminal_goal(self) -> None:
        terminal_goals = {
            "done": "",
            "failed": "goal failed",
            "cancelled": "goal cancelled",
        }
        for goal_status, error in terminal_goals.items():
            with self.subTest(goal_status=goal_status):
                goals = FakeGoalManager()
                goals.goals["goal-terminal"] = {
                    "goal_id": "goal-terminal",
                    "status": goal_status,
                    "error": error,
                }
                queue = RecordingQueue()
                queue.items["goal-terminal"] = FakeQueueItem("running")
                manager, _, _, _ = self._manager(
                    goal_manager=goals,
                    queue=queue,
                )

                result = await manager.cancel_goal(
                    "goal-terminal",
                    "late cancellation",
                )

                self.assertEqual(result["status"], goal_status)
                self.assertEqual(
                    queue.items["goal-terminal"].status,
                    goal_status,
                )
                self.assertEqual(goals.cancelled, [])


if __name__ == "__main__":
    unittest.main()
