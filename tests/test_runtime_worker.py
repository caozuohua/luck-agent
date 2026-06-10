from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from runtime.task_queue import RuntimeQueueItem, RuntimeTaskQueue
from runtime.worker import RuntimeWorker, WorkerManager


class FakeQueue:
    def __init__(self, events: list[tuple] | None = None) -> None:
        self.done: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.interrupted: list[tuple[str, str]] = []
        self.events = events

    async def get_item(self, goal_id: str) -> RuntimeQueueItem | None:
        return None

    async def mark_done(self, goal_id: str) -> bool:
        self.done.append(goal_id)
        if self.events is not None:
            self.events.append(("done", goal_id))
        return True

    async def mark_failed(self, goal_id: str, error: str) -> bool:
        self.failed.append((goal_id, error))
        if self.events is not None:
            self.events.append(("failed", goal_id, error))
        return True

    async def mark_cancelled(self, goal_id: str, reason: str) -> bool:
        self.cancelled.append((goal_id, reason))
        if self.events is not None:
            self.events.append(("cancelled", goal_id, reason))
        return True

    async def mark_interrupted(self, goal_id: str, reason: str) -> bool:
        self.interrupted.append((goal_id, reason))
        if self.events is not None:
            self.events.append(("interrupted", goal_id, reason))
        return True


class FakeGoalManager:
    def __init__(self, failed_goal: dict | None = None, *, raises: bool = False) -> None:
        self.failed_goal = failed_goal
        self.raises = raises
        self.calls: list[tuple[str, str]] = []
        self.pause_calls: list[tuple[str, str]] = []

    def fail_goal(self, goal_id: str, error: str) -> dict:
        self.calls.append((goal_id, error))
        if self.raises:
            raise RuntimeError("persistence unavailable")
        return {**(self.failed_goal or {}), "goal_id": goal_id}

    def pause_goal(self, goal_id: str, reason: str = "") -> dict:
        self.pause_calls.append((goal_id, reason))
        return {
            "goal_id": goal_id,
            "user_id": "u1",
            "chat_id": "c1",
            "status": "interrupted",
            "error": reason,
            "artifacts": [],
        }


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


class BlockingEngine:
    def __init__(self, goal_manager: FakeGoalManager) -> None:
        self.goal_manager = goal_manager
        self.started = asyncio.Event()

    async def run_goal(self, goal_id: str) -> dict:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class ControlledEngine:
    def __init__(self, result: dict, goal_manager: FakeGoalManager | None = None) -> None:
        self.result = result
        self.goal_manager = goal_manager or FakeGoalManager()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_goal(self, goal_id: str) -> dict:
        self.started.set()
        await self.release.wait()
        return {**self.result, "goal_id": goal_id}


class CancelledGoalManager(FakeGoalManager):
    def pause_goal(self, goal_id: str, reason: str = "") -> dict:
        self.pause_calls.append((goal_id, reason))
        raise RuntimeError("goal status cannot be paused: cancelled")

    def get_goal(self, goal_id: str) -> dict:
        return {
            "goal_id": goal_id,
            "user_id": "u1",
            "chat_id": "c1",
            "status": "cancelled",
            "error": "user cancelled",
            "artifacts": [],
        }


def queue_item() -> RuntimeQueueItem:
    return RuntimeQueueItem(goal_id="g1", user_id="u1", chat_id="c1")


class RuntimeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_preserves_goal_cancelled_during_pause_race(self) -> None:
        notified: list[dict] = []
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        goal_manager = CancelledGoalManager()
        engine = BlockingEngine(goal_manager)

        async def notify(goal: dict) -> None:
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=engine,
            terminal_callback=notify,
        )
        worker.start()
        await asyncio.wait_for(engine.started.wait(), timeout=1)
        self.assertTrue(await queue.cancel("g1", "user cancelled"))

        await worker.stop()
        await asyncio.wait_for(queue._queue.join(), timeout=1)

        item = await queue.get_item("g1")
        self.assertIsNotNone(item)
        self.assertEqual(item.status, "cancelled")
        self.assertEqual(goal_manager.calls, [])
        self.assertEqual(worker.state.failed, 0)
        self.assertEqual(notified, [])

    async def test_stop_interrupts_running_goal_and_finishes_real_queue_item(self) -> None:
        notified: list[dict] = []
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        goal_manager = FakeGoalManager()
        engine = BlockingEngine(goal_manager)

        async def notify(goal: dict) -> None:
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=engine,
            terminal_callback=notify,
        )
        worker.start()
        await asyncio.wait_for(engine.started.wait(), timeout=1)

        await worker.stop()
        await asyncio.wait_for(queue._queue.join(), timeout=1)

        snapshot = await queue.snapshot()
        self.assertEqual(snapshot["counts"], {"interrupted": 1})
        self.assertEqual(snapshot["items"][0]["error"], "worker stopped")
        self.assertEqual(goal_manager.pause_calls, [("g1", "worker stopped")])
        self.assertEqual(notified, [])
        self.assertEqual(worker.state.current_goal_id, "")

    async def test_queue_cancel_wins_over_engine_done_result(self) -> None:
        notified: list[dict] = []
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        engine = ControlledEngine({"status": "done", "artifacts": []})

        async def notify(goal: dict) -> None:
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=engine,
            terminal_callback=notify,
        )
        worker.start()
        await asyncio.wait_for(engine.started.wait(), timeout=1)

        self.assertTrue(await queue.cancel("g1", "user cancelled"))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(queue._queue.join(), timeout=0.01)
        engine.release.set()
        await asyncio.wait_for(queue._queue.join(), timeout=1)
        await worker.stop()

        item = await queue.get_item("g1")
        self.assertIsNotNone(item)
        self.assertEqual(item.status, "cancelled")
        self.assertEqual(item.error, "user cancelled")
        self.assertEqual(worker.state.processed, 0)
        self.assertEqual(worker.state.failed, 0)
        self.assertEqual(notified, [])

    async def test_worker_counts_only_successful_queue_transition(self) -> None:
        queue = FakeQueue()

        async def reject_done(goal_id: str) -> bool:
            queue.done.append(goal_id)
            return False

        queue.mark_done = reject_done
        notified: list[dict] = []

        async def notify(goal: dict) -> None:
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine({"status": "done", "artifacts": []}),
            terminal_callback=notify,
        )

        await worker._process_item(queue_item())

        self.assertEqual(worker.state.processed, 0)
        self.assertEqual(notified, [])

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

        with patch("runtime.worker.log") as runtime_log:
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
        runtime_log.error.assert_any_call(
            "runtime_goal_persist_failed",
            goal_id="g1",
            error="RuntimeError: persistence unavailable",
            original_error="ValueError: exploded",
        )

    async def test_cancelled_goal_preserves_queue_state_without_notification(self) -> None:
        notified: list[dict] = []
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        item = await queue.get()

        async def notify(goal: dict) -> None:
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine(
                {
                    "status": "cancelled",
                    "error": "user cancelled",
                    "artifacts": [],
                }
            ),
            terminal_callback=notify,
        )

        with patch("runtime.worker.log") as runtime_log:
            await worker._process_item(item)
        await asyncio.wait_for(queue._queue.join(), timeout=1)

        snapshot = await queue.snapshot()
        self.assertEqual(snapshot["counts"], {"cancelled": 1})
        self.assertEqual(snapshot["items"][0]["error"], "user cancelled")
        self.assertEqual(worker.state.processed, 0)
        self.assertEqual(worker.state.failed, 0)
        self.assertEqual(notified, [])
        failed_logs = [
            call
            for call in runtime_log.error.call_args_list
            if call.args and call.args[0] == "runtime_goal_failed"
        ]
        self.assertEqual(failed_logs, [])

    async def test_mark_cancelled_is_idempotent_for_running_item(self) -> None:
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        await queue.get()

        await queue.mark_cancelled("g1", "user cancelled")
        await queue.mark_cancelled("g1", "duplicate")
        await asyncio.wait_for(queue._queue.join(), timeout=1)

        snapshot = await queue.snapshot()
        self.assertEqual(snapshot["counts"], {"cancelled": 1})
        self.assertEqual(snapshot["items"][0]["error"], "user cancelled")

    async def test_mark_cancelled_finishes_pending_item_once(self) -> None:
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")

        await queue.mark_cancelled("g1", "user cancelled")
        await queue.mark_cancelled("g1", "duplicate")
        await asyncio.wait_for(queue._queue.join(), timeout=1)

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.01)

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
