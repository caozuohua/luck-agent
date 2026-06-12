from __future__ import annotations

import asyncio
import inspect
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent import AgentApp
from controllers.content_generator import GeneratedContent
from core.execution_engine import ExecutionEngine
from core.goal import GoalManager
from core.memory import Memory
from core.supervisor import Supervisor
from runtime.notifications import AcceptanceGatedNotifier
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


class ControlledSender:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.accepted_started = asyncio.Event()
        self.release_accepted = asyncio.Event()

    async def send(self, chat_id: str, **kwargs) -> None:
        self.calls.append("accepted_start")
        self.accepted_started.set()
        await self.release_accepted.wait()
        self.calls.append("accepted_done")


class RecordingNotifier:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def notify(self, goal: dict) -> None:
        self.calls.append("final")


class ImmediateEngine:
    def __init__(self, goal_manager: GoalManager) -> None:
        self.goal_manager = goal_manager

    async def run_goal(self, goal_id: str) -> dict:
        return self.goal_manager.complete_goal(
            goal_id,
            artifacts=[{
                "type": "generated_content",
                "content": "fast result",
                "model": "fast-engine",
                "tokens": 1,
            }],
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

        self.assertNotIn("BlogController", source)
        self.assertNotIn("register_controller", source)
        self.assertIn(
            "ModelContentGenerator(router=self._router, model_name=cfg.MODEL_PRO)",
            source,
        )
        self.assertIn(
            "registry = SkillRegistry(["
            "BlogSkill(generator=generator), LegacyReactSkill()])",
            " ".join(source.split()),
        )
        self.assertIn("skill_router = SkillRouter(registry)", source)
        self.assertIn(
            "event_recorder = RuntimeEventRecorder(self._memory)",
            source,
        )
        self.assertIn("skill_registry=registry", source)
        self.assertEqual(source.count("event_recorder=event_recorder"), 3)
        runtime_manager_source = source[
            source.index("self._runtime_manager = RuntimeManager("):
            source.index("notifier = RuntimeGoalNotifier(")
        ]
        self.assertIn("skill_router=skill_router", runtime_manager_source)
        worker_source = source[source.index("self._runtime_workers = WorkerManager("):]
        self.assertIn("event_recorder=event_recorder", worker_source)
        self.assertIn(
            "RuntimeGoalNotifier(sender=self._sender, card_builder=CardBuilder)",
            source,
        )
        self.assertIn("terminal_notifier = AcceptanceGatedNotifier(", source)
        self.assertIn("terminal_callback=terminal_notifier.notify", source)

    def test_agent_recovers_runtime_goals_before_starting_workers(self) -> None:
        source = inspect.getsource(AgentApp.run)

        recover_call = "await self._runtime_manager.recover_goals()"
        worker_start = "self._runtime_workers.start()"
        self.assertIn(recover_call, source)
        self.assertLess(source.index(recover_call), source.index(worker_start))

    def test_agent_uses_warning_level_for_lark_sdk_logs(self) -> None:
        source = inspect.getsource(AgentApp.run)

        self.assertIn("log_level=lark.LogLevel.WARNING", source)
        self.assertNotIn("log_level=lark.LogLevel.INFO", source)

    def test_agent_registers_all_configured_application_secrets(self) -> None:
        source = inspect.getsource(AgentApp.run)

        self.assertIn("self.cfg.LARK_APP_SECRET", source)
        self.assertIn("self.cfg.GITHUB_TOKEN", source)
        self.assertIn("self.cfg.TAVILY_API_KEY", source)
        self.assertIn("self.cfg.API_SECRET", source)

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

    async def test_fast_runtime_sends_accepted_before_final_notification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            queue = RuntimeTaskQueue(max_active=1)
            registry, router = blog_runtime_dependencies()
            engine = ImmediateEngine(goal_manager)
            manager = RuntimeManager(
                goal_manager=goal_manager,
                execution_engine=engine,
                queue=queue,
                skill_registry=registry,
                skill_router=router,
            )
            calls: list[str] = []
            sender = ControlledSender(calls)
            notifier = RecordingNotifier(calls)
            app = AgentApp.__new__(AgentApp)
            app.cfg = SimpleNamespace(
                ADMIN_USERS={"user-fast"},
                MODEL_PRO="",
                MODEL_FLASH="",
                MODEL_LITE="",
            )
            app._sender = sender
            app._runtime_manager = manager
            app._health = SimpleNamespace(mark_ws_ok=lambda: None)
            app._memory = SimpleNamespace(set_profile=lambda *args: None)

            async def no_command(*args) -> bool:
                return False

            app._cmd_handler = SimpleNamespace(handle=no_command)
            app._msg_handler = SimpleNamespace()
            app._file_handler = SimpleNamespace()
            terminal_notifier = AcceptanceGatedNotifier(
                wait_until_accepted=manager.wait_until_accepted,
                notifier=notifier,
            )
            workers = WorkerManager(
                queue=queue,
                execution_engine=engine,
                terminal_callback=terminal_notifier.notify,
            )
            workers.start()
            message_task = asyncio.create_task(app._on_message({
                "event": {
                    "message": {
                        "chat_id": "chat-fast",
                        "message_id": "message-fast",
                        "message_type": "text",
                        "chat_type": "p2p",
                        "content": '{"text":"帮我整理一个博客选题"}',
                    },
                    "sender": {"sender_id": {"open_id": "user-fast"}},
                },
            }))
            try:
                await asyncio.wait_for(sender.accepted_started.wait(), timeout=1)
                await asyncio.sleep(0.05)
                self.assertEqual(calls, ["accepted_start"])

                sender.release_accepted.set()
                await asyncio.wait_for(message_task, timeout=1)
                for _ in range(100):
                    if calls == ["accepted_start", "accepted_done", "final"]:
                        break
                    await asyncio.sleep(0.01)

                self.assertEqual(
                    calls,
                    ["accepted_start", "accepted_done", "final"],
                )
                self.assertEqual(calls.count("accepted_start"), 1)
                self.assertEqual(calls.count("final"), 1)
            finally:
                sender.release_accepted.set()
                await workers.stop()
                memory._local.conn.close()

    async def test_group_chat_rejects_direct_commands_before_dispatch(self) -> None:
        app = AgentApp.__new__(AgentApp)
        app.cfg = SimpleNamespace(
            ADMIN_USERS={"admin-user"},
            MODEL_PRO="",
            MODEL_FLASH="",
            MODEL_LITE="",
        )
        app._health = SimpleNamespace(mark_ws_ok=lambda: None)
        app._memory = SimpleNamespace(set_profile=lambda *args: None)
        app._sender = SimpleNamespace(send=AsyncMock())
        app._cmd_handler = SimpleNamespace(
            is_command=lambda text: text.startswith("/"),
            handle=AsyncMock(return_value=True),
        )

        await app._on_message({
            "event": {
                "message": {
                    "chat_id": "group-chat",
                    "message_id": "group-message",
                    "message_type": "text",
                    "chat_type": "group",
                    "mentions": [{"key": "@_user_1"}],
                    "content": '{"text":"@bot /runtime"}',
                },
                "sender": {"sender_id": {"open_id": "admin-user"}},
            },
        })

        app._cmd_handler.handle.assert_not_awaited()
        app._sender.send.assert_awaited_once()
        self.assertIn(
            "私聊",
            app._sender.send.await_args.kwargs["text"],
        )

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
            registry, router = blog_runtime_dependencies()
            engine = ExecutionEngine(
                goal_manager=goal_manager,
                supervisor=Supervisor(memory=memory),
                skill_registry=registry,
            )
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
            registry, router = blog_runtime_dependencies()
            engine = ExecutionEngine(
                goal_manager=goal_manager,
                supervisor=Supervisor(memory=memory),
                skill_registry=registry,
            )
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

    async def test_startup_recovery_immediately_requeues_running_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            goal_id = goal_manager.create_goal(
                user_id="crash-user",
                chat_id="crash-chat",
                title="recent crashed goal",
                intent="blog_write",
                status="running",
            )
            step_id = goal_manager.create_step(
                goal_id=goal_id,
                name="generate_content",
                input={
                    "name": "generate_content",
                    "action": "generate_content",
                    "replay_safe": True,
                },
            )
            _step, claimed = goal_manager.start_step(step_id)
            self.assertTrue(claimed)
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
                self.assertEqual(
                    goal_manager.get_goal(goal_id)["status"],
                    "interrupted",
                )
                self.assertEqual(
                    goal_manager.get_steps(goal_id)[0]["status"],
                    "pending",
                )
            finally:
                memory._local.conn.close()

    async def test_startup_recovery_blocks_unsafe_running_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "runtime.db"))
            goal_manager = GoalManager(memory)
            goal_id = goal_manager.create_goal(
                user_id="crash-user",
                chat_id="crash-chat",
                title="unsafe crashed goal",
                intent="blog_write",
                status="running",
            )
            step_id = goal_manager.create_step(
                goal_id=goal_id,
                name="publish_content",
                input={
                    "name": "publish_content",
                    "action": "publish_content",
                },
            )
            _step, claimed = goal_manager.start_step(step_id)
            self.assertTrue(claimed)
            queue = RuntimeTaskQueue(max_active=1)
            registry, router = blog_runtime_dependencies()
            manager = RuntimeManager(
                goal_manager=goal_manager,
                queue=queue,
                skill_registry=registry,
                skill_router=router,
            )

            try:
                self.assertEqual(await manager.recover_goals(), 0)
                self.assertEqual(
                    goal_manager.get_goal(goal_id)["status"],
                    "blocked",
                )
                step = goal_manager.get_steps(goal_id)[0]
                self.assertEqual(step["status"], "blocked")
                self.assertIn("replay", step["error"])
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
