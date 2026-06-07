from __future__ import annotations

import unittest

from runtime.intent_router import RuntimeIntentRouter
from runtime.runtime_manager import RuntimeManager
from runtime.task_queue import RuntimeTaskQueue


class FakeGoalManager:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create_goal_from_message(self, **kwargs) -> str:
        self.created.append(kwargs)
        return "goal-test"

    def summary(self, goal_id: str) -> str:
        return f"summary:{goal_id}"


class FailingExecutionEngine:
    async def run_goal(self, goal_id: str) -> None:
        raise AssertionError("RuntimeManager must not execute goals inline")


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_blog_topic_request_routes_to_goal_runtime(self) -> None:
        route = RuntimeIntentRouter().route("帮我整理一个博客选题")

        self.assertEqual(route.intent, "blog_write")
        self.assertTrue(route.use_goal_runtime)

    async def test_runtime_manager_submits_goal_without_inline_execution(self) -> None:
        goal_manager = FakeGoalManager()
        queue = RuntimeTaskQueue(max_active=1)
        manager = RuntimeManager(
            goal_manager=goal_manager,
            execution_engine=FailingExecutionEngine(),
            queue=queue,
        )

        result = await manager.handle_message(
            user_id="user-1",
            chat_id="chat-1",
            text="帮我整理一个博客选题",
        )

        self.assertTrue(result["handled"])
        self.assertEqual(result["goal_id"], "goal-test")
        self.assertEqual(goal_manager.created[0]["intent"], "blog_write")
        snapshot = await queue.snapshot()
        self.assertEqual(snapshot["counts"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
