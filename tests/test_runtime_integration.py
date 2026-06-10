from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path

from agent import AgentApp
from controllers.blog_controller import BlogController
from controllers.content_generator import GeneratedContent
from core.execution_engine import ExecutionEngine
from core.goal import GoalManager
from core.memory import Memory
from core.supervisor import Supervisor
from runtime.intent_router import RuntimeIntentRouter
from runtime.runtime_manager import RuntimeManager
from runtime.task_queue import RuntimeTaskQueue
from runtime.worker import WorkerManager


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


class EndToEndGenerator:
    async def generate(self, goal: dict) -> GeneratedContent:
        return GeneratedContent(
            text="选题：用 SQLite 构建可靠的轻量任务运行时",
            model="end-to-end-model",
            tokens=12,
        )


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_blog_topic_request_routes_to_goal_runtime(self) -> None:
        route = RuntimeIntentRouter().route("帮我整理一个博客选题")

        self.assertEqual(route.intent, "blog_write")
        self.assertTrue(route.use_goal_runtime)

    def test_agent_runtime_wires_final_result_dependencies(self) -> None:
        source = inspect.getsource(AgentApp._init_components)

        self.assertIn(
            "ModelContentGenerator(router=self._router, model_name=cfg.MODEL_PRO)",
            source,
        )
        self.assertIn("BlogController(generator=generator)", source)
        self.assertIn(
            "RuntimeGoalNotifier(sender=self._sender, card_builder=CardBuilder)",
            source,
        )
        self.assertIn("terminal_callback=notifier.notify", source)

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

    async def test_blog_topic_request_reaches_persisted_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            queue = RuntimeTaskQueue(max_active=1)
            engine = ExecutionEngine(
                goal_manager=goal_manager,
                supervisor=Supervisor(memory=memory),
            )
            engine.register_controller(
                BlogController(generator=EndToEndGenerator())
            )
            manager = RuntimeManager(
                goal_manager=goal_manager,
                execution_engine=engine,
                queue=queue,
            )
            terminal_goals: list[dict] = []

            async def collect_terminal_goal(goal: dict) -> None:
                terminal_goals.append(goal)

            workers = WorkerManager(
                queue=queue,
                execution_engine=engine,
                terminal_callback=collect_terminal_goal,
            )
            workers.start()
            try:
                result = await manager.handle_message(
                    user_id="user-e2e",
                    chat_id="chat-e2e",
                    text="帮我整理一个博客选题",
                )

                self.assertTrue(result["handled"])
                self.assertEqual(result["status"], "accepted")

                goal = None
                snapshot = None
                for _ in range(100):
                    goal = goal_manager.get_goal(result["goal_id"])
                    snapshot = await queue.snapshot()
                    if (
                        goal["status"] == "done"
                        and len(terminal_goals) == 1
                        and snapshot["counts"].get("done") == 1
                    ):
                        break
                    await asyncio.sleep(0.01)

                self.assertIsNotNone(goal)
                self.assertEqual(goal["status"], "done")
                self.assertEqual(len(terminal_goals), 1)
                self.assertEqual(terminal_goals[0]["status"], "done")
                self.assertEqual(
                    goal["artifacts"],
                    [{
                        "type": "generated_content",
                        "content": "选题：用 SQLite 构建可靠的轻量任务运行时",
                        "model": "end-to-end-model",
                        "tokens": 12,
                    }],
                )
                self.assertEqual(snapshot["counts"].get("done"), 1)
            finally:
                await workers.stop()
                memory._local.conn.close()


if __name__ == "__main__":
    unittest.main()
