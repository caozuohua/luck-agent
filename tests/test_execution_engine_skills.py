from __future__ import annotations

import asyncio
import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import core.execution_engine as execution_engine_module
from core.execution_engine import ExecutionEngine, StepResult, StepSpec
from core.goal import GoalManager
from core.memory import Memory
from core.supervisor import Supervisor, SupervisorDecision
from skills.base import (
    BlockingSkillError,
    FatalSkillError,
    RetryableSkillError,
    SkillMatch,
    SkillMetadata,
)
from skills.legacy_react import LegacyReactSkill
from skills.registry import SkillRegistry


class RecordingEventRecorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self.fail = fail

    def record(self, event_type: str, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("recorder unavailable")
        self.events.append({"event_type": event_type, **kwargs})


class FalseyEventRecorder(RecordingEventRecorder):
    def __bool__(self) -> bool:
        return False


class FakeGoalSkill:
    def __init__(
        self,
        *,
        name: str = "test_skill",
        intent: str = "test_intent",
        version: str = "2.3.4",
        plan: list[StepSpec] | None = None,
        results: list[StepResult | Exception] | None = None,
        delay: float = 0,
        always_incomplete: bool = False,
        build_error: Exception | None = None,
        completion_error: Exception | None = None,
        build_started: asyncio.Event | None = None,
        build_release: asyncio.Event | None = None,
        execute_started: asyncio.Event | None = None,
        execute_release: asyncio.Event | None = None,
    ) -> None:
        self.metadata = SkillMetadata(
            name=name,
            version=version,
            intent=intent,
            description="execution engine test skill",
            execution_mode="goal_runtime",
            timeout=1,
            max_retry=1,
        )
        self.plan = (
            [StepSpec(name="work", action="work", timeout=1, max_retry=1)]
            if plan is None
            else plan
        )
        self.results = list(results or [StepResult(ok=True, action="work")])
        self.delay = delay
        self.always_incomplete = always_incomplete
        self.build_error = build_error
        self.completion_error = completion_error
        self.build_started = build_started
        self.build_release = build_release
        self.execute_started = execute_started
        self.execute_release = execute_release
        self.build_calls = 0
        self.execute_calls = 0
        self.complete_calls = 0

    def match(self, context) -> SkillMatch:
        return SkillMatch(False)

    def build_goal(self, context):
        raise AssertionError("not used by execution engine")

    async def build_plan(self, goal: dict[str, Any]) -> list[StepSpec]:
        self.build_calls += 1
        if self.build_started:
            self.build_started.set()
        if self.build_release:
            await self.build_release.wait()
        if self.build_error:
            raise self.build_error
        return list(self.plan)

    async def execute_step(
        self,
        goal: dict[str, Any],
        step: StepSpec,
    ) -> StepResult:
        self.execute_calls += 1
        if self.execute_started:
            self.execute_started.set()
        if self.execute_release:
            await self.execute_release.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def is_goal_complete(
        self,
        goal: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> bool:
        self.complete_calls += 1
        if self.completion_error:
            raise self.completion_error
        if self.always_incomplete:
            return False
        return bool(steps) and all(
            step.get("status") == "done"
            for step in steps
            if (step.get("input") or {}).get("required", True)
        )


class FixedDecisionSupervisor(Supervisor):
    def __init__(self, decision: str, reason: str) -> None:
        super().__init__()
        self.decision = decision
        self.reason = reason

    def review_step_result(self, **kwargs) -> SupervisorDecision:
        return SupervisorDecision(
            decision=self.decision,
            verification={},
            reason=self.reason,
        )


class RaisingSupervisor(Supervisor):
    def __init__(self, *, phase: str, error: Exception) -> None:
        super().__init__()
        self.phase = phase
        self.error = error

    def review_step_result(self, **kwargs) -> SupervisorDecision:
        if self.phase == "step":
            raise self.error
        return super().review_step_result(**kwargs)

    def review_goal_completion(self, **kwargs) -> dict[str, Any]:
        if self.phase == "completion":
            raise self.error
        return super().review_goal_completion(**kwargs)


class ExecutionEngineSkillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.memory = Memory(str(Path(self.temp_dir.name) / "runtime.db"))
        self.goal_manager = GoalManager(self.memory)

    def tearDown(self) -> None:
        conn = getattr(self.memory._local, "conn", None)
        if conn is not None:
            conn.close()
            self.memory._local.conn = None
        self.temp_dir.cleanup()

    def create_goal(
        self,
        *,
        intent: str = "test_intent",
        plan: dict[str, Any] | None = None,
    ) -> str:
        return self.goal_manager.create_goal(
            user_id="user-1",
            chat_id="chat-1",
            title="test goal",
            intent=intent,
            plan=plan,
        )

    def engine(
        self,
        *skills,
        recorder: RecordingEventRecorder | None = None,
        supervisor: Supervisor | None = None,
        **kwargs,
    ) -> ExecutionEngine:
        return ExecutionEngine(
            goal_manager=self.goal_manager,
            skill_registry=SkillRegistry(skills),
            event_recorder=recorder,
            supervisor=supervisor,
            **kwargs,
        )

    def event_types(self, recorder: RecordingEventRecorder) -> list[str]:
        return [event["event_type"] for event in recorder.events]

    def test_skill_registry_is_required_and_controller_api_is_removed(self) -> None:
        with self.assertRaises(TypeError):
            ExecutionEngine(goal_manager=self.goal_manager)

        source = inspect.getsource(execution_engine_module)
        self.assertNotIn("GoalController", source)
        self.assertNotIn("register_controller", source)
        self.assertNotIn("get_controller", source)
        self.assertNotIn("controllers", source)

    async def test_engine_resolves_persisted_skill_identity_before_intent(self) -> None:
        selected = FakeGoalSkill(name="persisted", intent="selected")
        by_intent = FakeGoalSkill(name="intent_skill", intent="old_intent")
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(
            intent="old_intent",
            plan={"skill": "persisted", "skill_version": "1.0.0"},
        )

        goal = await self.engine(
            selected,
            by_intent,
            recorder=recorder,
        ).run_goal(goal_id)

        self.assertEqual(goal["status"], "done")
        self.assertEqual(selected.execute_calls, 1)
        self.assertEqual(by_intent.execute_calls, 0)
        self.assertTrue(all(event["skill"] == "persisted" for event in recorder.events))
        self.assertTrue(
            all(event["payload"]["skill_version"] == "2.3.4" for event in recorder.events)
        )

    async def test_engine_falls_back_to_unique_intent_for_old_goal(self) -> None:
        skill = FakeGoalSkill()
        goal_id = self.create_goal(plan={"source_message": "old"})

        goal = await self.engine(skill).run_goal(goal_id)

        self.assertEqual(goal["status"], "done")
        self.assertEqual(skill.execute_calls, 1)

    async def test_terminal_goal_returns_before_skill_resolution_or_events(self) -> None:
        for status in ("done", "failed", "cancelled", "blocked"):
            with self.subTest(status=status):
                recorder = RecordingEventRecorder()
                goal_id = self.create_goal(
                    intent="missing_intent",
                    plan={"skill": "missing_skill"},
                )
                self.memory.update_goal(goal_id, status=status, error="original")

                goal = await self.engine(recorder=recorder).run_goal(goal_id)

                self.assertEqual(goal["status"], status)
                self.assertEqual(goal["error"], "original")
                self.assertEqual(recorder.events, [])

    async def test_running_goal_is_not_executed_by_second_engine(self) -> None:
        skill = FakeGoalSkill()
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        self.memory.update_goal(goal_id, status="running")

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "running")
        self.assertEqual(skill.build_calls, 0)
        self.assertEqual(skill.execute_calls, 0)
        self.assertEqual(recorder.events, [])

    async def test_sqlite_claim_allows_only_one_engine_to_execute_goal(self) -> None:
        db_path = str(Path(self.temp_dir.name) / "runtime.db")
        other_memory = Memory(db_path)
        other_goal_manager = GoalManager(other_memory)
        skill = FakeGoalSkill()
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        engine_one = self.engine(skill, recorder=recorder)
        engine_two = ExecutionEngine(
            goal_manager=other_goal_manager,
            skill_registry=SkillRegistry([skill]),
            event_recorder=recorder,
        )
        barrier = threading.Barrier(2)

        async def run(engine: ExecutionEngine) -> dict:
            await asyncio.to_thread(barrier.wait)
            return await engine.run_goal(goal_id)

        try:
            first, second = await asyncio.gather(run(engine_one), run(engine_two))

            self.assertIn("done", {first["status"], second["status"]})
            self.assertTrue(
                {first["status"], second["status"]}.issubset({"running", "done"})
            )
            self.assertEqual(skill.build_calls, 1)
            self.assertEqual(skill.execute_calls, 1)
            self.assertEqual(len(self.goal_manager.get_steps(goal_id)), 1)
            self.assertEqual(self.goal_manager.get_goal(goal_id)["status"], "done")
            self.assertEqual(self.event_types(recorder).count("goal.started"), 1)
            self.assertEqual(self.event_types(recorder).count("goal.completed"), 1)
        finally:
            other_conn = getattr(other_memory._local, "conn", None)
            if other_conn is not None:
                other_conn.close()
                other_memory._local.conn = None

    async def test_missing_corrupt_and_legacy_skills_block_without_leaking(self) -> None:
        secret = "secret-api-key"
        cases = (
            (SkillRegistry(), {"skill": "removed_skill"}),
            (SkillRegistry(), {"skill": {"token": secret}}),
            (SkillRegistry([LegacyReactSkill()]), {"skill": "legacy_react"}),
        )
        for registry, plan in cases:
            with self.subTest(plan=plan):
                recorder = RecordingEventRecorder()
                goal_id = self.create_goal(intent="general", plan=plan)
                engine = ExecutionEngine(
                    goal_manager=self.goal_manager,
                    skill_registry=registry,
                    event_recorder=recorder,
                )

                goal = await engine.run_goal(goal_id)

                self.assertEqual(goal["status"], "blocked")
                self.assertEqual(goal["error"], "missing skill")
                self.assertEqual(self.event_types(recorder), ["goal.blocked"])
                self.assertEqual(
                    recorder.events[0]["payload"],
                    {
                        "error_type": "SkillNotFoundError",
                        "skill_version": "",
                    },
                )
                self.assertNotIn(secret, repr(recorder.events))

    async def test_plan_creation_records_ordered_lifecycle_events(self) -> None:
        skill = FakeGoalSkill(
            plan=[
                StepSpec(name="first", action="first"),
                StepSpec(name="second", action="second"),
            ],
            results=[
                StepResult(ok=True, action="first"),
                StepResult(ok=True, action="second"),
            ],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "done")
        self.assertEqual(
            self.event_types(recorder),
            [
                "goal.started",
                "step.created",
                "step.created",
                "step.started",
                "supervisor.decision",
                "step.completed",
                "step.started",
                "supervisor.decision",
                "step.completed",
                "goal.completed",
            ],
        )
        decisions = [
            event for event in recorder.events
            if event["event_type"] == "supervisor.decision"
        ]
        self.assertEqual(
            decisions[0]["payload"],
            {
                "decision": "pass",
                "reason": "step passed",
                "retry_count": 0,
                "max_retry": 1,
                "skill_version": "2.3.4",
            },
        )

    async def test_preexisting_steps_do_not_emit_duplicate_created_events(self) -> None:
        skill = FakeGoalSkill()
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        self.goal_manager.create_step(
            goal_id=goal_id,
            name="work",
            input=skill.plan[0].to_dict(),
        )

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "done")
        self.assertNotIn("step.created", self.event_types(recorder))
        self.assertEqual(skill.build_calls, 0)

    async def test_cancel_during_build_plan_creates_no_steps_or_events(self) -> None:
        build_started = asyncio.Event()
        build_release = asyncio.Event()
        skill = FakeGoalSkill(
            plan=[
                StepSpec(name="first", action="first"),
                StepSpec(name="second", action="second"),
            ],
            build_started=build_started,
            build_release=build_release,
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        task = asyncio.create_task(
            self.engine(skill, recorder=recorder).run_goal(goal_id)
        )
        await build_started.wait()

        self.goal_manager.cancel_goal(goal_id)
        build_release.set()
        goal = await task

        self.assertEqual(goal["status"], "cancelled")
        self.assertEqual(goal["current_step"], "")
        self.assertEqual(self.goal_manager.get_steps(goal_id), [])
        self.assertNotIn("step.created", self.event_types(recorder))
        self.assertNotIn("goal.blocked", self.event_types(recorder))

    async def test_atomic_plan_commit_allows_only_one_plan(self) -> None:
        db_path = str(Path(self.temp_dir.name) / "runtime.db")
        other_memory = Memory(db_path)
        other_goal_manager = GoalManager(other_memory)
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        self.memory.update_goal(goal_id, status="running")
        plan = [
            StepSpec(name="first", action="first"),
            StepSpec(name="second", action="second"),
        ]
        barrier = threading.Barrier(2)

        async def commit(manager: GoalManager) -> tuple[list[dict], bool]:
            def commit_in_thread() -> tuple[list[dict], bool]:
                try:
                    barrier.wait()
                    return manager.commit_plan(goal_id, plan)
                finally:
                    conn = getattr(manager.memory._local, "conn", None)
                    if conn is not None:
                        conn.close()
                        manager.memory._local.conn = None

            return await asyncio.to_thread(commit_in_thread)

        first, second = await asyncio.gather(
            commit(self.goal_manager),
            commit(other_goal_manager),
        )

        self.assertEqual(sum(1 for _, committed in (first, second) if committed), 1)
        self.assertEqual(len(self.goal_manager.get_steps(goal_id)), 2)
        self.assertEqual(
            [step["name"] for step in self.goal_manager.get_steps(goal_id)],
            ["first", "second"],
        )
        self.assertEqual(
            self.goal_manager.get_goal(goal_id)["current_step"],
            "first",
        )
        other_conn = getattr(other_memory._local, "conn", None)
        if other_conn is not None:
            other_conn.close()
            other_memory._local.conn = None

    async def test_retry_records_one_retry_mutation_and_then_completes(self) -> None:
        skill = FakeGoalSkill(
            results=[
                StepResult(ok=False, action="work", error="try again"),
                StepResult(ok=True, action="work"),
            ],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "done")
        self.assertEqual(skill.execute_calls, 2)
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["retry_count"], 1)
        self.assertEqual(self.event_types(recorder).count("step.retry"), 1)
        retry_event = next(
            event for event in recorder.events
            if event["event_type"] == "step.retry"
        )
        self.assertEqual(retry_event["payload"]["retry_count"], 1)

    async def test_fail_decision_fails_step_and_goal(self) -> None:
        skill = FakeGoalSkill()
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(
            skill,
            recorder=recorder,
            supervisor=FixedDecisionSupervisor("fail", "terminal failure"),
        ).run_goal(goal_id)

        self.assertEqual(goal["status"], "failed")
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "failed")
        self.assertEqual(
            self.event_types(recorder)[-2:],
            ["step.failed", "goal.failed"],
        )

    async def test_block_decision_blocks_step_and_goal(self) -> None:
        skill = FakeGoalSkill()
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(
            skill,
            recorder=recorder,
            supervisor=FixedDecisionSupervisor("block", "needs input"),
        ).run_goal(goal_id)

        self.assertEqual(goal["status"], "blocked")
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "blocked")
        self.assertEqual(step["error"], "needs input")
        self.assertTrue(step["finished_at"])
        self.assertEqual(
            self.event_types(recorder)[-2:],
            ["step.blocked", "goal.blocked"],
        )

    async def test_retryable_skill_error_retries_then_completes(self) -> None:
        skill = FakeGoalSkill(
            results=[
                RetryableSkillError("provider busy", hint="retry later"),
                StepResult(ok=True, action="work"),
            ],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "done")
        self.assertEqual(skill.execute_calls, 2)
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["retry_count"], 1)
        self.assertEqual(step["status"], "done")
        self.assertEqual(self.event_types(recorder).count("step.retry"), 1)

    async def test_retryable_skill_error_exhausts_budget_and_blocks(self) -> None:
        skill = FakeGoalSkill(
            results=[RetryableSkillError("provider busy", hint="retry later")],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "blocked")
        self.assertEqual(skill.execute_calls, 2)
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["retry_count"], 1)
        self.assertEqual(step["status"], "blocked")
        self.assertEqual(step["error"], "provider busy")

    async def test_blocking_skill_error_blocks_without_retry(self) -> None:
        skill = FakeGoalSkill(
            results=[
                BlockingSkillError(
                    "permission required",
                    hint="grant permission",
                )
            ],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "blocked")
        self.assertEqual(skill.execute_calls, 1)
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["retry_count"], 0)
        self.assertEqual(step["status"], "blocked")
        self.assertEqual(step["error"], "permission required")

    async def test_fatal_skill_error_fails_without_supervisor_decision(self) -> None:
        skill = FakeGoalSkill(
            results=[FatalSkillError("invalid skill state")],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "failed")
        self.assertEqual(goal["error"], "invalid skill state")
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "failed")
        self.assertEqual(step["error"], "invalid skill state")
        self.assertNotIn("supervisor.decision", self.event_types(recorder))
        self.assertEqual(
            self.event_types(recorder)[-2:],
            ["step.failed", "goal.failed"],
        )
        failed_event = recorder.events[-2]
        self.assertEqual(failed_event["payload"]["error_type"], "FatalSkillError")
        self.assertEqual(failed_event["payload"]["error_class"], "fatal")

    async def test_unknown_skill_error_fails_with_sanitized_type(self) -> None:
        secret = "private failure"
        skill = FakeGoalSkill(results=[ValueError(secret)])
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        expected = "skill execute failed: ValueError"
        self.assertEqual(goal["status"], "failed")
        self.assertEqual(goal["error"], expected)
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "failed")
        self.assertEqual(step["error"], expected)
        self.assertNotIn("supervisor.decision", self.event_types(recorder))
        failed_event = next(
            event
            for event in recorder.events
            if event["event_type"] == "step.failed"
        )
        self.assertEqual(failed_event["payload"]["error_type"], "ValueError")
        self.assertEqual(failed_event["payload"]["error_class"], "fatal")
        self.assertNotIn(
            secret,
            repr({
                "goal": goal,
                "steps": self.goal_manager.get_steps(goal_id),
                "events": recorder.events,
                "lessons": self.memory.search_lessons(task_type="test_intent"),
            }),
        )

    async def test_slow_skill_retries_once_before_timeout_blocks(self) -> None:
        timeout_skill = FakeGoalSkill(
            plan=[StepSpec(name="work", action="work", timeout=0)],
            delay=0.05,
        )
        timeout_recorder = RecordingEventRecorder()
        timeout_id = self.create_goal(plan={"skill": "test_skill"})

        timeout_goal = await self.engine(
            timeout_skill,
            recorder=timeout_recorder,
            default_step_timeout=0.01,
        ).run_goal(timeout_id)

        self.assertEqual(timeout_goal["status"], "blocked")
        self.assertEqual(timeout_skill.execute_calls, 2)
        timeout_step = self.goal_manager.get_steps(timeout_id)[0]
        self.assertEqual(timeout_step["retry_count"], 1)
        self.assertEqual(timeout_step["status"], "blocked")
        self.assertEqual(
            self.event_types(timeout_recorder)[-4:],
            [
                "step.started",
                "supervisor.decision",
                "step.blocked",
                "goal.blocked",
            ],
        )

    async def test_skill_raised_timeout_error_is_fatal_and_sanitized(self) -> None:
        secret = "provider timeout secret"
        skill = FakeGoalSkill(results=[asyncio.TimeoutError(secret)])
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        expected = "skill execute failed: TimeoutError"
        self.assertEqual(goal["status"], "failed")
        self.assertEqual(goal["error"], expected)
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "failed")
        self.assertEqual(step["error"], expected)
        self.assertNotIn("supervisor.decision", self.event_types(recorder))
        failed_event = next(
            event
            for event in recorder.events
            if event["event_type"] == "step.failed"
        )
        self.assertEqual(failed_event["payload"]["error_type"], "TimeoutError")
        self.assertEqual(failed_event["payload"]["error_class"], "fatal")
        self.assertNotIn(
            secret,
            repr({
                "goal": goal,
                "step": step,
                "events": recorder.events,
            }),
        )

    async def test_cancel_during_execute_keeps_goal_cancelled(self) -> None:
        execute_started = asyncio.Event()
        execute_release = asyncio.Event()
        skill = FakeGoalSkill(
            results=[
                StepResult(
                    ok=True,
                    action="work",
                    artifacts=[{"kind": "secret", "value": "artifact"}],
                )
            ],
            execute_started=execute_started,
            execute_release=execute_release,
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        task = asyncio.create_task(
            self.engine(skill, recorder=recorder).run_goal(goal_id)
        )
        await execute_started.wait()

        self.goal_manager.cancel_goal(goal_id)
        execute_release.set()
        goal = await task

        self.assertEqual(goal["status"], "cancelled")
        self.assertEqual(goal["artifacts"], [])
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "running")
        self.assertEqual(step["error"], "")
        self.assertNotIn("step.blocked", self.event_types(recorder))
        self.assertNotIn("step.completed", self.event_types(recorder))
        self.assertNotIn("goal.completed", self.event_types(recorder))

    async def test_cancel_before_start_step_is_not_overwritten(self) -> None:
        skill = FakeGoalSkill()
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        original_start_step = self.goal_manager.start_step

        def cancel_then_start(step_id: str) -> dict:
            self.goal_manager.cancel_goal(goal_id)
            return original_start_step(step_id)

        with patch.object(
            self.goal_manager,
            "start_step",
            side_effect=cancel_then_start,
        ):
            goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "cancelled")
        self.assertEqual(skill.execute_calls, 0)
        self.assertEqual(self.goal_manager.get_steps(goal_id)[0]["status"], "pending")
        self.assertNotIn("step.started", self.event_types(recorder))

    async def test_atomic_start_step_rejects_cancelled_goal_and_keeps_pending(self) -> None:
        goal_id = self.create_goal(plan={"skill": "test_skill"})
        step_id = self.goal_manager.create_step(
            goal_id=goal_id,
            name="work",
            input=StepSpec(name="work", action="work").to_dict(),
        )
        self.goal_manager.cancel_goal(goal_id)

        step, claimed = self.goal_manager.start_step(step_id)

        self.assertFalse(claimed)
        self.assertEqual(step["status"], "pending")
        self.assertIsNone(step["started_at"])
        self.assertEqual(self.goal_manager.get_goal(goal_id)["status"], "cancelled")

    async def test_cancel_before_pass_commit_prevents_step_and_artifact_commit(self) -> None:
        commit_started = asyncio.Event()
        commit_release = asyncio.Event()
        skill = FakeGoalSkill(
            results=[
                StepResult(
                    ok=True,
                    action="work",
                    artifacts=[{"kind": "file", "path": "private.txt"}],
                )
            ],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        async def delayed_commit(*args, **kwargs) -> bool:
            commit_started.set()
            await commit_release.wait()
            return await original_commit(*args, **kwargs)

        original_commit = self.goal_manager.commit_step_result
        with patch.object(
            self.goal_manager,
            "commit_step_result",
            side_effect=delayed_commit,
        ):
            task = asyncio.create_task(
                self.engine(skill, recorder=recorder).run_goal(goal_id)
            )
            await commit_started.wait()
            self.goal_manager.cancel_goal(goal_id)
            commit_release.set()
            goal = await task

        self.assertEqual(goal["status"], "cancelled")
        self.assertEqual(goal["artifacts"], [])
        self.assertEqual(self.goal_manager.get_steps(goal_id)[0]["status"], "running")
        self.assertNotIn("step.completed", self.event_types(recorder))
        self.assertNotIn("goal.completed", self.event_types(recorder))

    async def test_cancel_before_retry_commit_prevents_retry_mutation_and_event(self) -> None:
        commit_started = asyncio.Event()
        commit_release = asyncio.Event()
        skill = FakeGoalSkill(
            results=[StepResult(ok=False, action="work", error="retry")],
        )
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        async def delayed_commit(*args, **kwargs) -> bool:
            commit_started.set()
            await commit_release.wait()
            return await original_commit(*args, **kwargs)

        original_commit = self.goal_manager.commit_step_result
        with patch.object(
            self.goal_manager,
            "commit_step_result",
            side_effect=delayed_commit,
        ):
            task = asyncio.create_task(
                self.engine(skill, recorder=recorder).run_goal(goal_id)
            )
            await commit_started.wait()
            self.goal_manager.cancel_goal(goal_id)
            commit_release.set()
            goal = await task

        self.assertEqual(goal["status"], "cancelled")
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "running")
        self.assertEqual(step["retry_count"], 0)
        self.assertNotIn("step.retry", self.event_types(recorder))
        self.assertNotIn("goal.blocked", self.event_types(recorder))

    async def test_build_plan_exception_is_sanitized_and_does_not_leave_running(self) -> None:
        secret = "build-secret"
        skill = FakeGoalSkill(build_error=RuntimeError(secret))
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "failed")
        self.assertEqual(goal["error"], "skill build failed: RuntimeError")
        self.assertEqual(recorder.events[-1]["payload"]["error_type"], "RuntimeError")
        self.assertNotIn(secret, repr({"goal": goal, "events": recorder.events}))

    async def test_completion_exceptions_are_sanitized_and_do_not_leave_running(self) -> None:
        cases = (
            (
                FakeGoalSkill(completion_error=RuntimeError("completion-secret")),
                None,
                "skill completion failed: RuntimeError",
                "completion-secret",
            ),
            (
                FakeGoalSkill(),
                RaisingSupervisor(
                    phase="completion",
                    error=ValueError("supervisor-secret"),
                ),
                "supervisor completion failed: ValueError",
                "supervisor-secret",
            ),
        )
        for skill, supervisor, expected_error, secret in cases:
            with self.subTest(expected_error=expected_error):
                recorder = RecordingEventRecorder()
                goal_id = self.create_goal(plan={"skill": "test_skill"})

                goal = await self.engine(
                    skill,
                    supervisor=supervisor,
                    recorder=recorder,
                ).run_goal(goal_id)

                self.assertEqual(goal["status"], "failed")
                self.assertEqual(goal["error"], expected_error)
                self.assertEqual(recorder.events[-1]["event_type"], "goal.failed")
                self.assertNotIn(
                    secret,
                    repr({
                        "goal": goal,
                        "steps": self.goal_manager.get_steps(goal_id),
                        "events": recorder.events,
                    }),
                )

    async def test_step_supervisor_exception_is_sanitized_and_does_not_leave_running(self) -> None:
        secret = "step-supervisor-secret"
        skill = FakeGoalSkill()
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(
            skill,
            recorder=recorder,
            supervisor=RaisingSupervisor(
                phase="step",
                error=RuntimeError(secret),
            ),
        ).run_goal(goal_id)

        self.assertEqual(goal["status"], "failed")
        self.assertEqual(goal["error"], "supervisor step failed: RuntimeError")
        step = self.goal_manager.get_steps(goal_id)[0]
        self.assertEqual(step["status"], "failed")
        self.assertNotIn(
            secret,
            repr({
                "goal": goal,
                "step": step,
                "events": recorder.events,
                "lessons": self.memory.search_lessons(task_type="test_intent"),
            }),
        )

    async def test_empty_plan_blocks_goal_instead_of_raising(self) -> None:
        skill = FakeGoalSkill(plan=[])
        recorder = RecordingEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertEqual(goal["status"], "blocked")
        self.assertEqual(goal["error"], "empty plan")
        self.assertEqual(
            self.event_types(recorder),
            ["goal.started", "goal.blocked"],
        )

    async def test_no_pending_step_and_max_steps_record_goal_blocked(self) -> None:
        no_pending_skill = FakeGoalSkill(always_incomplete=True)
        no_pending_recorder = RecordingEventRecorder()
        no_pending_id = self.create_goal(plan={"skill": "test_skill"})
        step_id = self.goal_manager.create_step(
            goal_id=no_pending_id,
            name="already done",
            input=StepSpec(name="already done", action="done").to_dict(),
        )
        self.goal_manager.finish_step(step_id)

        no_pending_goal = await self.engine(
            no_pending_skill,
            recorder=no_pending_recorder,
        ).run_goal(no_pending_id)

        self.assertEqual(no_pending_goal["status"], "blocked")
        self.assertEqual(
            no_pending_recorder.events[-1]["event_type"],
            "goal.blocked",
        )

        retrying_skill = FakeGoalSkill(
            plan=[StepSpec(name="work", action="work", max_retry=5)],
            results=[StepResult(ok=False, action="work", error="retry")],
        )
        budget_recorder = RecordingEventRecorder()
        budget_id = self.create_goal(plan={"skill": "test_skill"})

        budget_goal = await self.engine(
            retrying_skill,
            recorder=budget_recorder,
            max_steps=1,
        ).run_goal(budget_id)

        self.assertEqual(budget_goal["status"], "blocked")
        self.assertIn("max step budget exceeded", budget_goal["error"])
        self.assertEqual(
            self.event_types(budget_recorder)[-2:],
            ["step.retry", "goal.blocked"],
        )

    async def test_recorder_failure_never_interrupts_execution(self) -> None:
        skill = FakeGoalSkill()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        goal = await self.engine(
            skill,
            recorder=RecordingEventRecorder(fail=True),
        ).run_goal(goal_id)

        self.assertEqual(goal["status"], "done")

    async def test_falsey_recorder_is_not_replaced_by_noop_default(self) -> None:
        skill = FakeGoalSkill()
        recorder = FalseyEventRecorder()
        goal_id = self.create_goal(plan={"skill": "test_skill"})

        await self.engine(skill, recorder=recorder).run_goal(goal_id)

        self.assertIn("goal.completed", self.event_types(recorder))


if __name__ == "__main__":
    unittest.main()
