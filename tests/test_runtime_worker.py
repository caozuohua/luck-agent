from __future__ import annotations

import unittest
from unittest.mock import patch

from runtime.task_queue import RuntimeQueueItem
from runtime.worker import RuntimeWorker, WorkerManager


class FakeQueue:
    def __init__(self, events: list[tuple] | None = None) -> None:
        self.done: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.events = events

    async def mark_done(self, goal_id: str) -> None:
        self.done.append(goal_id)
        if self.events is not None:
            self.events.append(("done", goal_id))

    async def mark_failed(self, goal_id: str, error: str) -> None:
        self.failed.append((goal_id, error))
        if self.events is not None:
            self.events.append(("failed", goal_id, error))


class FakeGoalManager:
    def __init__(self, failed_goal: dict | None = None, *, raises: bool = False) -> None:
        self.failed_goal = failed_goal
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def fail_goal(self, goal_id: str, error: str) -> dict:
        self.calls.append((goal_id, error))
        if self.raises:
            raise RuntimeError("persistence unavailable")
        return {**(self.failed_goal or {}), "goal_id": goal_id}


class FakeEngine:
    def __init__(
        self,
        result: dict | None = None,
        *,
        error: Exception | None = None,
        goal_manager: FakeGoalManager | None = None,
    ) -> None:
        self.result = result or {}
        self.error = error
        self.goal_manager = goal_manager or FakeGoalManager()

    async def run_goal(self, goal_id: str) -> dict:
        if self.error:
            raise self.error
        return {**self.result, "goal_id": goal_id}


def queue_item() -> RuntimeQueueItem:
    return RuntimeQueueItem(goal_id="g1", user_id="u1", chat_id="c1")


class RuntimeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_done_goal_marks_queue_done_then_notifies_once(self) -> None:
        events: list[tuple] = []
        queue = FakeQueue(events)

        async def notify(goal: dict) -> None:
            events.append(("notify", goal["goal_id"], goal["status"]))

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine({"status": "done", "artifacts": []}),
            terminal_callback=notify,
        )

        with patch("runtime.worker.log") as runtime_log:
            await worker._process_item(queue_item())

        self.assertEqual(events, [("done", "g1"), ("notify", "g1", "done")])
        self.assertEqual(queue.failed, [])
        self.assertEqual(worker.state.processed, 1)
        self.assertEqual(worker.state.failed, 0)
        runtime_log.info.assert_any_call(
            "runtime_goal_done",
            worker_id="w1",
            goal_id="g1",
        )

    async def test_blocked_goal_marks_queue_failed_then_notifies_once(self) -> None:
        events: list[tuple] = []
        queue = FakeQueue(events)

        async def notify(goal: dict) -> None:
            events.append(("notify", goal["goal_id"], goal["status"]))

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine(
                {
                    "status": "blocked",
                    "error": "model unavailable",
                    "artifacts": [],
                }
            ),
            terminal_callback=notify,
        )

        with patch("runtime.worker.log") as runtime_log:
            await worker._process_item(queue_item())

        self.assertEqual(
            events,
            [
                ("failed", "g1", "model unavailable"),
                ("notify", "g1", "blocked"),
            ],
        )
        self.assertEqual(queue.done, [])
        self.assertEqual(worker.state.processed, 0)
        self.assertEqual(worker.state.failed, 1)
        runtime_log.error.assert_any_call(
            "runtime_goal_failed",
            worker_id="w1",
            goal_id="g1",
            status="blocked",
            error="model unavailable",
        )

    async def test_engine_exception_persists_and_notifies_failed_goal(self) -> None:
        notified: list[dict] = []
        goal_manager = FakeGoalManager(
            {
                "user_id": "u1",
                "chat_id": "c1",
                "status": "failed",
                "error": "ValueError: exploded",
                "artifacts": [{"kind": "log"}],
            }
        )
        queue = FakeQueue()

        async def notify(goal: dict) -> None:
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine(
                error=ValueError("exploded"),
                goal_manager=goal_manager,
            ),
            terminal_callback=notify,
        )

        await worker._process_item(queue_item())

        self.assertEqual(goal_manager.calls, [("g1", "ValueError: exploded")])
        self.assertEqual(queue.failed, [("g1", "ValueError: exploded")])
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["status"], "failed")
        self.assertEqual(notified[0]["artifacts"], [{"kind": "log"}])

    async def test_engine_and_persistence_exception_notifies_fallback_goal(self) -> None:
        notified: list[dict] = []
        queue = FakeQueue()

        async def notify(goal: dict) -> None:
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine(
                error=ValueError("exploded"),
                goal_manager=FakeGoalManager(raises=True),
            ),
            terminal_callback=notify,
        )

        await worker._process_item(queue_item())

        self.assertEqual(
            notified,
            [
                {
                    "goal_id": "g1",
                    "user_id": "u1",
                    "chat_id": "c1",
                    "status": "failed",
                    "error": "ValueError: exploded",
                    "artifacts": [],
                }
            ],
        )

    async def test_callback_exception_does_not_change_terminal_state(self) -> None:
        queue = FakeQueue()
        calls = 0

        async def notify(goal: dict) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("delivery failed")

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine({"status": "done", "artifacts": []}),
            terminal_callback=notify,
        )

        with patch("runtime.worker.log") as runtime_log:
            await worker._process_item(queue_item())

        self.assertEqual(calls, 1)
        self.assertEqual(queue.done, ["g1"])
        self.assertEqual(queue.failed, [])
        self.assertEqual(worker.state.processed, 1)
        self.assertEqual(worker.state.failed, 0)
        self.assertEqual(worker.state.last_error, "")
        runtime_log.error.assert_called_once_with(
            "runtime_terminal_notify_failed",
            goal_id="g1",
            status="done",
            error="delivery failed",
        )

    def test_worker_manager_propagates_terminal_callback(self) -> None:
        async def notify(goal: dict) -> None:
            return None

        manager = WorkerManager(
            queue=FakeQueue(),
            execution_engine=FakeEngine(),
            worker_count=2,
            terminal_callback=notify,
        )

        self.assertEqual(len(manager.workers), 2)
        self.assertTrue(all(worker.terminal_callback is notify for worker in manager.workers))


if __name__ == "__main__":
    unittest.main()
