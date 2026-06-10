from __future__ import annotations

import asyncio
import unittest

from runtime.task_queue import RuntimeTaskQueue


class RuntimeTaskQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_running_waits_for_worker_to_finish_item(self) -> None:
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        await queue.get()

        self.assertTrue(await queue.cancel("g1", "user cancelled"))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(queue._queue.join(), timeout=0.01)

        self.assertTrue(await queue.mark_cancelled("g1", "user cancelled"))
        self.assertFalse(await queue.mark_cancelled("g1", "duplicate"))
        await asyncio.wait_for(queue._queue.join(), timeout=1)

    async def test_terminal_markers_return_true_exactly_once(self) -> None:
        methods = (
            ("mark_done", ()),
            ("mark_failed", ("failed",)),
            ("mark_cancelled", ("cancelled",)),
            ("mark_interrupted", ("interrupted",)),
        )
        for index, (method_name, args) in enumerate(methods):
            with self.subTest(method=method_name):
                queue = RuntimeTaskQueue()
                goal_id = f"g{index}"
                await queue.submit(goal_id=goal_id, user_id="u1", chat_id="c1")
                await queue.get()
                method = getattr(queue, method_name)

                self.assertTrue(await method(goal_id, *args))
                self.assertFalse(await method(goal_id, *args))
                await asyncio.wait_for(queue._queue.join(), timeout=1)

    async def test_duplicate_active_submit_returns_existing_item_without_extra_put(self) -> None:
        queue = RuntimeTaskQueue()
        first = await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        duplicate = await queue.submit(goal_id="g1", user_id="u2", chat_id="c2")

        self.assertIs(duplicate, first)
        item = await queue.get()
        self.assertIs(item, first)
        self.assertTrue(await queue.mark_done("g1"))
        await asyncio.wait_for(queue._queue.join(), timeout=1)

    async def test_duplicate_terminal_submit_raises_without_leaking_join(self) -> None:
        queue = RuntimeTaskQueue()
        await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        await queue.get()
        self.assertTrue(await queue.mark_done("g1"))

        with self.assertRaises(ValueError):
            await queue.submit(goal_id="g1", user_id="u1", chat_id="c1")
        await asyncio.wait_for(queue._queue.join(), timeout=1)


if __name__ == "__main__":
    unittest.main()
