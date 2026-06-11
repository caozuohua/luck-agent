from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from controllers.content_generator import GeneratedContent
from core.execution_engine import ExecutionEngine
from core.goal import GoalManager
from core.memory import Memory
from core.supervisor import Supervisor
from runtime.events import RuntimeEventRecorder
from runtime.notifications import AcceptanceGatedNotifier
from runtime.runtime_manager import RuntimeManager
from runtime.task_queue import RuntimeTaskQueue
from runtime.worker import WorkerManager
from skills.blog import BlogSkill
from skills.legacy_react import LegacyReactSkill
from skills.registry import SkillRegistry
from skills.router import SkillRouter


class FakeGenerator:
    async def generate(self, goal: dict) -> GeneratedContent:
        return GeneratedContent(
            text="选题：SQLite 任务运行时",
            model="fake-e2e-model",
            tokens=9,
        )


class SkillRuntimeEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.memory = Memory(str(Path(self.temp_dir.name) / "runtime.db"))
        self.goal_manager = GoalManager(self.memory)
        self.queue = RuntimeTaskQueue(max_active=1)
        self.registry = SkillRegistry([
            BlogSkill(generator=FakeGenerator()),
            LegacyReactSkill(),
        ])
        self.router = SkillRouter(self.registry)
        self.recorder = RuntimeEventRecorder(self.memory)
        self.engine = ExecutionEngine(
            goal_manager=self.goal_manager,
            supervisor=Supervisor(memory=self.memory),
            skill_registry=self.registry,
            event_recorder=self.recorder,
        )
        self.manager = RuntimeManager(
            goal_manager=self.goal_manager,
            execution_engine=self.engine,
            queue=self.queue,
            skill_registry=self.registry,
            skill_router=self.router,
            event_recorder=self.recorder,
        )
        self.workers: WorkerManager | None = None

    async def asyncTearDown(self) -> None:
        if self.workers is not None:
            await self.workers.stop()
        self.memory._local.conn.close()
        self.temp_dir.cleanup()

    async def _wait_for_event(
        self,
        goal_id: str,
        event_type: str,
        *,
        attempts: int = 200,
    ) -> list[dict]:
        for _ in range(attempts):
            events = self.memory.list_runtime_events(goal_id=goal_id)
            if any(event["event_type"] == event_type for event in events):
                return events
            await asyncio.sleep(0.01)
        self.fail(f"event not recorded: {event_type}")

    async def test_blog_request_completes_with_authoritative_event_order(self) -> None:
        notified: list[str] = []

        async def notify(goal: dict) -> None:
            notified.append(goal["goal_id"])

        self.workers = WorkerManager(
            queue=self.queue,
            execution_engine=self.engine,
            terminal_callback=notify,
            event_recorder=self.recorder,
        )
        self.workers.start()

        result = await self.manager.handle_message(
            user_id="user-e2e",
            chat_id="chat-e2e",
            text="帮我整理一个博客选题",
        )
        events = await self._wait_for_event(
            result["goal_id"],
            "notification.sent",
        )
        events = self.memory.list_runtime_events()
        goal = self.goal_manager.get_goal(result["goal_id"])

        self.assertTrue(result["handled"])
        self.assertEqual(result["skill"], "blog_write")
        self.assertEqual(result["intent"], "blog_write")
        self.assertTrue(result["goal_id"])
        self.assertIn("状态：pending", result["summary"])
        self.assertEqual(goal["status"], "done")
        self.assertEqual(goal["plan"]["skill"], "blog_write")
        self.assertEqual(goal["plan"]["skill_version"], "1.0.0")
        self.assertEqual(notified, [result["goal_id"]])
        self.assertEqual(
            [event["event_type"] for event in events],
            [
                "route.matched",
                "goal.created",
                "goal.accepted",
                "queue.submitted",
                "worker.pickup",
                "goal.started",
                "step.created",
                "step.started",
                "supervisor.decision",
                "step.completed",
                "goal.completed",
                "queue.completed",
                "notification.sent",
            ],
        )

    async def test_production_notifier_waits_for_accepted_send_completion(
        self,
    ) -> None:
        calls: list[str] = []
        final_sent = asyncio.Event()

        class FinalNotifier:
            async def notify(_self, goal: dict) -> None:
                calls.append("final")
                final_sent.set()

        terminal = AcceptanceGatedNotifier(
            wait_until_accepted=self.manager.wait_until_accepted,
            notifier=FinalNotifier(),
        )
        self.workers = WorkerManager(
            queue=self.queue,
            execution_engine=self.engine,
            terminal_callback=terminal.notify,
            event_recorder=self.recorder,
        )
        self.workers.start()

        result = await self.manager.handle_message(
            user_id="user-gated",
            chat_id="chat-gated",
            text="帮我整理一个博客选题",
        )
        calls.append("accepted_start")
        await asyncio.sleep(0.05)
        self.assertFalse(final_sent.is_set())

        calls.append("accepted_done")
        self.manager.mark_accepted(result["goal_id"])
        await asyncio.wait_for(final_sent.wait(), timeout=1)

        self.assertEqual(calls, ["accepted_start", "accepted_done", "final"])

    async def test_general_route_falls_back_without_creating_goal(self) -> None:
        result = await self.manager.handle_message(
            user_id="user-general",
            chat_id="chat-general",
            text="今天天气怎么样",
        )

        self.assertFalse(result["handled"])
        self.assertEqual(result["skill"], "legacy_react")
        self.assertEqual(result["intent"], "general")
        self.assertEqual(result["goal_id"], "")
        self.assertEqual(self.goal_manager.list_goals(), [])
        self.assertEqual(
            [
                event["event_type"]
                for event in self.memory.list_runtime_events()
            ],
            ["route.fallback"],
        )

    async def test_old_intent_only_goal_recovers_and_completes(self) -> None:
        goal_id = self.goal_manager.create_goal(
            user_id="user-old",
            chat_id="chat-old",
            title="旧博客 Goal",
            intent="blog_write",
            plan={"source_message": "帮我整理一个博客选题"},
            status="interrupted",
        )
        self.workers = WorkerManager(
            queue=self.queue,
            execution_engine=self.engine,
            event_recorder=self.recorder,
        )

        self.assertEqual(await self.manager.recover_goals(), 1)
        self.workers.start()
        await self._wait_for_event(goal_id, "queue.completed")

        goal = self.goal_manager.get_goal(goal_id)
        self.assertEqual(goal["status"], "done")
        self.assertEqual(goal["intent"], "blog_write")

    async def test_notification_exception_records_sanitized_failure(self) -> None:
        async def fail_notification(goal: dict) -> None:
            raise RuntimeError("private delivery detail")

        self.workers = WorkerManager(
            queue=self.queue,
            execution_engine=self.engine,
            terminal_callback=fail_notification,
            event_recorder=self.recorder,
        )
        self.workers.start()

        result = await self.manager.handle_message(
            user_id="user-fail",
            chat_id="chat-fail",
            text="帮我整理一个博客选题",
        )
        events = await self._wait_for_event(
            result["goal_id"],
            "notification.failed",
        )
        failed = events[-1]

        self.assertEqual(failed["event_type"], "notification.failed")
        self.assertEqual(failed["payload"], {"error_type": "RuntimeError"})
        self.assertNotIn("private delivery detail", repr(events))
        self.assertEqual(
            self.goal_manager.get_goal(result["goal_id"])["status"],
            "done",
        )


if __name__ == "__main__":
    unittest.main()
