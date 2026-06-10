from __future__ import annotations

import asyncio
import inspect
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import AgentApp
from controllers.blog_controller import BlogController
from controllers.content_generator import GeneratedContent
from core.execution_engine import ExecutionEngine
from core.goal import GoalManager
from core.memory import Memory
from core.supervisor import Supervisor
from runtime.runtime_manager import RuntimeManager
from runtime.task_queue import RuntimeTaskQueue
from runtime.worker import WorkerManager
from skills.blog import BlogSkill
from skills.base import SkillContext
from skills.legacy_react import LegacyReactSkill
from skills.registry import SkillRegistry
from skills.router import SkillRouter


class FakeGoalManager:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create_goal(self, **kwargs) -> str:
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


def blog_runtime_dependencies(
    generator: EndToEndGenerator | None = None,
) -> tuple[SkillRegistry, SkillRouter]:
    registry = SkillRegistry(
        [
            BlogSkill(generator=generator or EndToEndGenerator()),
            LegacyReactSkill(),
        ]
    )
    return registry, SkillRouter(registry)


class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_blog_topic_request_routes_to_goal_runtime(self) -> None:
        _, router = blog_runtime_dependencies()
        route = router.route(
            SkillContext(
                user_id="route-user",
                chat_id="route-chat",
                text="帮我整理一个博客选题",
            )
        )

        self.assertEqual(route.intent, "blog_write")
        self.assertEqual(route.skill.metadata.name, "blog_write")
        self.assertEqual(route.execution_mode, "goal_runtime")

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

    def test_agent_recovers_runtime_goals_before_starting_workers(self) -> None:
        source = inspect.getsource(AgentApp.run)

        recover_call = "await self._runtime_manager.recover_goals()"
        worker_start = "self._runtime_workers.start()"
        self.assertIn(recover_call, source)
        self.assertLess(source.index(recover_call), source.index(worker_start))

    async def test_runtime_manager_submits_goal_without_inline_execution(self) -> None:
        goal_manager = FakeGoalManager()
        queue = RuntimeTaskQueue(max_active=1)
        registry, router = blog_runtime_dependencies()
        manager = RuntimeManager(
            goal_manager=goal_manager,
            execution_engine=FailingExecutionEngine(),
            queue=queue,
            skill_registry=registry,
            skill_router=router,
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

    async def test_runtime_manager_cancels_pending_goal_in_sqlite_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            queue = RuntimeTaskQueue(max_active=1)
            registry, router = blog_runtime_dependencies()
            manager = RuntimeManager(
                goal_manager=goal_manager,
                queue=queue,
                skill_registry=registry,
                skill_router=router,
            )
            goal_id = goal_manager.create_goal(
                user_id="cancel-user",
                chat_id="cancel-chat",
                title="cancel pending goal",
                intent="blog_write",
            )
            await queue.submit(
                goal_id=goal_id,
                user_id="cancel-user",
                chat_id="cancel-chat",
            )

            try:
                goal = await manager.cancel_goal(goal_id, "user cancelled")
                snapshot = await queue.snapshot()

                self.assertEqual(goal["status"], "cancelled")
                self.assertEqual(goal["error"], "user cancelled")
                self.assertEqual(goal_manager.get_goal(goal_id)["status"], "cancelled")
                self.assertEqual(snapshot["counts"], {"cancelled": 1})
                self.assertEqual(snapshot["items"][0]["error"], "user cancelled")
                await asyncio.wait_for(queue._queue.join(), timeout=1)
            finally:
                memory._local.conn.close()

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
            registry, router = blog_runtime_dependencies()
            manager = RuntimeManager(
                goal_manager=goal_manager,
                execution_engine=engine,
                queue=queue,
                skill_registry=registry,
                skill_router=router,
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

    async def test_recover_persisted_goals_requeues_and_completes_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            pending_id = goal_manager.create_goal(
                user_id="pending-user",
                chat_id="pending-chat",
                title="pending goal",
                intent="blog_write",
            )
            interrupted_id = goal_manager.create_goal(
                user_id="interrupted-user",
                chat_id="interrupted-chat",
                title="interrupted goal",
                intent="blog_write",
                status="interrupted",
            )
            done_id = goal_manager.create_goal(
                user_id="done-user",
                chat_id="done-chat",
                title="done goal",
                intent="blog_write",
                status="done",
            )
            queue = RuntimeTaskQueue(max_active=1)
            engine = ExecutionEngine(
                goal_manager=goal_manager,
                supervisor=Supervisor(memory=memory),
            )
            engine.register_controller(BlogController(generator=EndToEndGenerator()))
            registry, router = blog_runtime_dependencies()
            manager = RuntimeManager(
                goal_manager=goal_manager,
                execution_engine=engine,
                queue=queue,
                skill_registry=registry,
                skill_router=router,
            )
            terminal_goals: list[dict] = []

            async def collect_terminal_goal(goal: dict) -> None:
                terminal_goals.append(goal)

            workers = WorkerManager(
                queue=queue,
                execution_engine=engine,
                terminal_callback=collect_terminal_goal,
            )
            try:
                with patch("runtime.runtime_manager.log") as runtime_log:
                    recovered = await manager.recover_goals()
                    duplicate_recovery = await manager.recover_goals()

                self.assertEqual(recovered, 2)
                self.assertEqual(duplicate_recovery, 0)
                snapshot = await queue.snapshot()
                self.assertEqual(snapshot["counts"], {"pending": 2})
                recovered_items = {
                    item["goal_id"]: item
                    for item in snapshot["items"]
                }
                self.assertEqual(
                    set(recovered_items),
                    {pending_id, interrupted_id},
                )
                self.assertEqual(
                    recovered_items[pending_id]["meta"]["intent"],
                    "blog_write",
                )
                self.assertEqual(
                    (
                        recovered_items[pending_id]["user_id"],
                        recovered_items[pending_id]["chat_id"],
                    ),
                    ("pending-user", "pending-chat"),
                )
                self.assertNotIn(done_id, recovered_items)
                runtime_log.info.assert_any_call(
                    "runtime_goals_recovered",
                    count=2,
                )

                workers.start()
                for _ in range(200):
                    snapshot = await queue.snapshot()
                    if (
                        snapshot["counts"].get("done") == 2
                        and len(terminal_goals) == 2
                    ):
                        break
                    await asyncio.sleep(0.01)

                self.assertEqual(snapshot["counts"], {"done": 2})
                self.assertEqual(
                    {goal["goal_id"] for goal in terminal_goals},
                    {pending_id, interrupted_id},
                )
                self.assertEqual(goal_manager.get_goal(done_id)["status"], "done")
            finally:
                await workers.stop()
                memory._local.conn.close()

    async def test_recover_marks_stale_running_goal_interrupted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            stale_id = goal_manager.create_goal(
                user_id="stale-user",
                chat_id="stale-chat",
                title="stale goal",
                intent="blog_write",
                status="running",
            )
            with memory._conn() as conn:
                conn.execute(
                    "UPDATE goals SET updated_at=? WHERE goal_id=?",
                    (time.time() - 600, stale_id),
                )
            queue = RuntimeTaskQueue(max_active=1)
            registry, router = blog_runtime_dependencies()
            manager = RuntimeManager(
                goal_manager=goal_manager,
                queue=queue,
                skill_registry=registry,
                skill_router=router,
            )

            try:
                self.assertEqual(await manager.recover_goals(), 1)
                snapshot = await queue.snapshot()
                self.assertEqual(snapshot["counts"], {"pending": 1})
                self.assertEqual(snapshot["items"][0]["goal_id"], stale_id)
                self.assertEqual(
                    goal_manager.get_goal(stale_id)["status"],
                    "interrupted",
                )
            finally:
                memory._local.conn.close()

    async def test_recover_scans_more_than_one_page_of_pending_goals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            pending_ids: set[str] = set()
            with patch("core.goal.log"):
                for index in range(125):
                    pending_ids.add(
                        goal_manager.create_goal(
                            user_id=f"user-{index}",
                            chat_id=f"chat-{index}",
                            title=f"pending goal {index}",
                            intent="blog_write",
                        )
                    )
                done_id = goal_manager.create_goal(
                    user_id="done-user",
                    chat_id="done-chat",
                    title="done goal",
                    intent="blog_write",
                    status="done",
                )
            queue = RuntimeTaskQueue(max_active=1)
            registry, router = blog_runtime_dependencies()
            manager = RuntimeManager(
                goal_manager=goal_manager,
                queue=queue,
                skill_registry=registry,
                skill_router=router,
            )

            try:
                self.assertEqual(await manager.recover_goals(), 125)
                self.assertEqual(await manager.recover_goals(), 0)

                snapshot = await queue.snapshot()
                self.assertEqual(snapshot["counts"], {"pending": 125})
                recovered_ids = {
                    item["goal_id"]
                    for item in snapshot["items"]
                }
                self.assertEqual(recovered_ids, pending_ids)
                self.assertNotIn(done_id, recovered_ids)
            finally:
                memory._local.conn.close()


if __name__ == "__main__":
    unittest.main()
