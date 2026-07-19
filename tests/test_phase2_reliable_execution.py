from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from core.agent import AgentState, MinimalAgent
from core.intent_classifier import IntentClassifier
from core.output_parser import IntentType
from core.router import ToolRouter
from core.tool_executor import ToolExecutor
from memory.db import Database
from memory.goal_store import GoalStatus, GoalStore
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


@contextmanager
def _safe_tempdir():
    """TemporaryDirectory that retries cleanup on Windows file-lock races.

    Windows holds SQLite -wal/-shm handles briefly after close(), so rmtree
    on __exit__ can raise PermissionError (WinError 32). Retry with backoff.
    """
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        for _ in range(50):
            try:
                shutil.rmtree(d, ignore_errors=False)
                break
            except OSError:
                time.sleep(0.05)


class DummyTool(Tool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} tool"
        self.args_schema = {"type": "object"}

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult.ok(
            data={"text": kwargs.get("text", "done")},
            tool_name=self.name,
        )


class FailingTool(Tool):
    name = "failing_tool"
    description = "Always raises."
    args_schema = {"type": "object"}

    async def run(self, **kwargs: object) -> ToolResult:
        raise RuntimeError("provider down")


class ActionLLM:
    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return """
        {"intent":"ACTION","plan":"echo text","tool_call":{"name":"general_search","args":{"text":"done"}},"fallback":"report failure"}
        """


class RecordingGoalStore(GoalStore):
    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.transitions: list[GoalStatus] = []

    def schedule_status_update(
        self,
        goal_id: str,
        status: GoalStatus,
        **kwargs: object,
    ) -> asyncio.Task[None]:
        self.transitions.append(status)
        return super().schedule_status_update(goal_id, status, **kwargs)


class Phase2IntentClassifierTests(unittest.TestCase):
    def test_classifier_uses_rules_without_llm(self) -> None:
        classifier = IntentClassifier()

        self.assertEqual(classifier.classify("hello, how are you?"), IntentType.CHAT)
        self.assertEqual(classifier.classify("帮我安排明天的会议"), IntentType.ACTION)
        self.assertEqual(classifier.classify("这个怎么弄"), IntentType.CLARIFY)


class Phase2RouterTests(unittest.TestCase):
    def test_router_returns_registered_tool_subset_under_10ms_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules_path = Path(tmp) / "routing_rules.yaml"
            rules_path.write_text(
                """
rules:
  - name: "search"
    patterns: ["search"]
    tools: ["general_search", "file_search", "show_capabilities"]
fallback_tools:
  - "show_capabilities"
  - "ask_clarification"
  - "general_search"
""".strip(),
                encoding="utf-8",
            )
            registry = ToolRegistry(
                DummyTool(name)
                for name in (
                    "general_search",
                    "file_search",
                    "show_capabilities",
                    "ask_clarification",
                    "calendar_query",
                    "calendar_create",
                )
            )
            router = ToolRouter(registry, rules_path=rules_path)

            started = time.perf_counter()
            tools = router.route("please search docs", IntentType.ACTION)
            elapsed_ms = (time.perf_counter() - started) * 1000

            self.assertLess(elapsed_ms, 10)
            self.assertEqual([tool.name for tool in tools], [
                "general_search",
                "file_search",
                "show_capabilities",
            ])

            rules_path.write_text(
                """
rules:
  - name: "search"
    patterns: ["search"]
    tools: ["calendar_query", "calendar_create", "general_search"]
fallback_tools:
  - "show_capabilities"
""".strip(),
                encoding="utf-8",
            )
            router.reload_rules()

            self.assertEqual(
                [tool.name for tool in router.route("search calendar", IntentType.ACTION)],
                ["calendar_query", "calendar_create", "general_search"],
            )


class Phase2GoalStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_initializes_required_tables_and_fts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()

            rows = await db.fetchall(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
            names = {row["name"] for row in rows}

            self.assertIn("goals", names)
            self.assertIn("patterns", names)
            self.assertIn("context_summaries", names)
            self.assertIn("patterns_fts", names)
            await db.close()

    async def test_goal_store_crud_and_in_progress_recovery(self) -> None:
        with _safe_tempdir() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            store = GoalStore(db)

            goal = await store.create("user-1", "do work")
            await store.update_status(goal.id, GoalStatus.ROUTING, intent_type="ACTION")
            # Follow the legal state-machine chain (ROUTING -> PLANNING ->
            # EVALUATING -> DONE) as the runtime does in core/agent.py.
            await store.update_status(goal.id, GoalStatus.PLANNING)
            await store.update_status(goal.id, GoalStatus.EVALUATING)
            await store.update_status(goal.id, GoalStatus.DONE, result="ok")

            recent = await store.get_recent("user-1", limit=1)
            in_progress = await store.get_in_progress("user-1")

            self.assertEqual(recent[0].status, GoalStatus.DONE)
            self.assertEqual(recent[0].result, "ok")
            self.assertEqual(in_progress, [])
            await db.close()

    async def test_tool_executor_writes_error_pattern_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            registry = ToolRegistry([FailingTool()])
            executor = ToolExecutor(
                registry,
                error_pattern_writer=db.insert_pattern,
            )

            result = await executor.execute("failing_tool", {"x": 1})
            await executor.drain_pending_patterns()
            rows = await db.fetchall("SELECT * FROM patterns WHERE pattern_type = 'error'")

            self.assertEqual(result.status, "error")
            self.assertEqual(result.error, "provider down")
            self.assertEqual(rows[0]["tool_name"], "failing_tool")
            self.assertIn("provider down", rows[0]["outcome"])
            await db.close()


class Phase2StateMachineSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_goal_flows_from_idle_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            goal_store = RecordingGoalStore(db)
            registry = ToolRegistry([DummyTool("general_search")])
            router = ToolRouter(registry)
            agent = MinimalAgent(
                llm_client=ActionLLM(),
                tool_registry=registry,
                router=router,
                goal_store=goal_store,
                history_summary="",
                experience_patterns=[],
            )

            response = await agent.run_turn("please search", user_id="user-1")
            await goal_store.drain_pending()
            recent = await goal_store.get_recent("user-1", limit=1)

            self.assertIn("done", response)
            self.assertEqual(agent.state, AgentState.DONE)
            self.assertEqual(
                goal_store.transitions,
                [
                    GoalStatus.ROUTING,
                    GoalStatus.PLANNING,
                    GoalStatus.EXECUTING,
                    GoalStatus.AWAITING_RESULT,
                    GoalStatus.EVALUATING,
                    GoalStatus.DONE,
                ],
            )
            self.assertEqual(recent[0].status, GoalStatus.DONE)
            self.assertEqual(recent[0].intent_type, "ACTION")
            self.assertIn("general_search", recent[0].tool_calls)
            await db.close()


if __name__ == "__main__":
    unittest.main()
