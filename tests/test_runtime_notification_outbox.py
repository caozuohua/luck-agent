from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core.goal import GoalManager
from core.memory import Memory
from runtime.task_queue import RuntimeQueueItem
from runtime.task_queue import RuntimeTaskQueue
from runtime.worker import RuntimeWorker, WorkerManager


class FakeQueue:
    pass


class FakeGoalManager:
    def get_goal(self, goal_id: str) -> dict:
        return {
            "goal_id": goal_id,
            "user_id": "u1",
            "chat_id": "c1",
            "status": "done",
            "artifacts": [],
        }


class FakeEngine:
    def __init__(self) -> None:
        self.goal_manager = FakeGoalManager()


def queue_item(goal_id: str = "g1") -> RuntimeQueueItem:
    return RuntimeQueueItem(
        goal_id=goal_id,
        user_id="u1",
        chat_id="c1",
        meta={"skill": "blog_write", "intent": "blog_write"},
    )


class RuntimeNotificationOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.memory = Memory(str(Path(self.temp_dir.name) / "runtime.db"))

    def tearDown(self) -> None:
        connection = getattr(self.memory._local, "conn", None)
        if connection is not None:
            connection.close()
        self.temp_dir.cleanup()

    async def test_overlapping_workers_send_terminal_notification_once(self) -> None:
        calls = 0
        other_memory = Memory(self.memory.db_path)

        async def notify(goal: dict) -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)

        workers = [
            RuntimeWorker(
                worker_id=f"w{index}",
                queue=FakeQueue(),
                execution_engine=FakeEngine(),
                terminal_callback=notify,
                notification_store=store,
            )
            for index, store in enumerate((self.memory, other_memory))
        ]
        goal = FakeGoalManager().get_goal("g1")

        try:
            await asyncio.gather(
                *(
                    worker._notify_terminal_goal(queue_item(), goal, "done")
                    for worker in workers
                )
            )
        finally:
            connection = getattr(other_memory._local, "conn", None)
            if connection is not None:
                connection.close()

        self.assertEqual(calls, 1)
        notification = self.memory.get_goal_notification("g1")
        self.assertIsNotNone(notification)
        self.assertEqual(notification["state"], "sent")
        self.assertEqual(notification["attempts"], 1)

    async def test_failed_notification_can_be_claimed_for_retry(self) -> None:
        attempts = 0

        async def notify(goal: dict) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("delivery failed")

        worker = RuntimeWorker(
            worker_id="w1",
            queue=FakeQueue(),
            execution_engine=FakeEngine(),
            terminal_callback=notify,
            notification_store=self.memory,
        )
        goal = FakeGoalManager().get_goal("g1")

        await worker._notify_terminal_goal(queue_item(), goal, "done")
        self.assertEqual(self.memory.get_goal_notification("g1")["state"], "failed")

        await worker._notify_terminal_goal(queue_item(), goal, "done")

        self.assertEqual(attempts, 2)
        notification = self.memory.get_goal_notification("g1")
        self.assertEqual(notification["state"], "sent")
        self.assertEqual(notification["attempts"], 2)

    async def test_sent_notification_is_not_replayed(self) -> None:
        calls = 0

        async def notify(goal: dict) -> None:
            nonlocal calls
            calls += 1

        worker = RuntimeWorker(
            worker_id="w1",
            queue=FakeQueue(),
            execution_engine=FakeEngine(),
            terminal_callback=notify,
            notification_store=self.memory,
        )
        goal = FakeGoalManager().get_goal("g1")

        await worker._notify_terminal_goal(queue_item(), goal, "done")
        await worker._notify_terminal_goal(queue_item(), goal, "done")

        self.assertEqual(calls, 1)
        self.assertEqual(self.memory.get_goal_notification("g1")["attempts"], 1)

    async def test_worker_manager_recovers_pending_notification(self) -> None:
        goal_manager = GoalManager(self.memory)
        goal_id = goal_manager.create_goal(
            user_id="u1",
            chat_id="c1",
            title="recover notification",
            intent="blog_write",
            plan={"skill": "blog_write"},
        )
        goal_manager.complete_goal(goal_id)
        self.memory.ensure_goal_notification(goal_id, "done")
        notified = asyncio.Event()

        async def notify(goal: dict) -> None:
            self.assertEqual(goal["goal_id"], goal_id)
            notified.set()

        engine = FakeEngine()
        engine.goal_manager = goal_manager
        manager = WorkerManager(
            queue=FakeQueue(),
            execution_engine=engine,
            terminal_callback=notify,
            notification_store=self.memory,
        )

        recovered = await manager.recover_notifications()
        await asyncio.wait_for(notified.wait(), timeout=1)
        await manager.stop()

        self.assertEqual(recovered, 1)
        self.assertEqual(
            self.memory.get_goal_notification(goal_id)["state"],
            "sent",
        )

    def test_recoverable_notifications_exclude_sent_and_fresh_claims(self) -> None:
        for goal_id in ("pending", "failed", "claimed", "sent"):
            self.memory.ensure_goal_notification(goal_id, "done")
        self.memory.claim_goal_notification("failed")
        self.memory.mark_goal_notification_failed("failed", "TimeoutError")
        self.memory.claim_goal_notification("claimed")
        self.memory.claim_goal_notification("sent")
        self.memory.mark_goal_notification_sent("sent")

        recoverable = self.memory.list_recoverable_goal_notifications(
            stale_after_seconds=300,
        )

        self.assertEqual(
            {item["goal_id"] for item in recoverable},
            {"pending", "failed"},
        )

    async def test_worker_manager_retries_claim_abandoned_by_crashed_process(
        self,
    ) -> None:
        goal_manager = GoalManager(self.memory)
        goal_id = goal_manager.create_goal(
            user_id="u1",
            chat_id="c1",
            title="recover abandoned claim",
            intent="blog_write",
            plan={"skill": "blog_write"},
        )
        goal_manager.complete_goal(goal_id)
        self.memory.ensure_goal_notification(goal_id, "done")
        self.assertTrue(self.memory.claim_goal_notification(goal_id))
        notified = asyncio.Event()

        async def notify(goal: dict) -> None:
            notified.set()

        engine = FakeEngine()
        engine.goal_manager = goal_manager
        manager = WorkerManager(
            queue=RuntimeTaskQueue(),
            execution_engine=engine,
            terminal_callback=notify,
            notification_store=self.memory,
            notification_recovery_interval=0.01,
            notification_claim_timeout=0.01,
        )
        manager.start()
        try:
            await asyncio.wait_for(notified.wait(), timeout=1)
        finally:
            await manager.stop()

        notification = self.memory.get_goal_notification(goal_id)
        self.assertEqual(notification["state"], "sent")
        self.assertEqual(notification["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
