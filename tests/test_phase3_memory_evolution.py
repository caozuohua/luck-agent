from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.agent import MinimalAgent
from core.router import ToolRouter
from memory.context_store import ContextStore
from memory.curator import Curator
from memory.db import Database
from memory.goal_store import GoalStore
from memory.pattern_store import PatternStore
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


class SearchTool(Tool):
    name = "general_search"
    description = "Search test corpus."
    args_schema = {"type": "object"}

    async def run(self, **kwargs: object) -> ToolResult:
        text = str(kwargs.get("text", ""))
        return ToolResult.ok(
            data={"answer": f"found {text}"},
            tool_name=self.name,
        )


class Phase3LLM:
    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        self.prompts.append((system_prompt, task_prompt))
        if "Compress the middle conversation history" in task_prompt:
            return "compressed middle summary"
        if "Compress patterns into MEMORY.md" in task_prompt:
            return "Use general_search for repeat search tasks. Provider failures should be reported honestly."
        return (
            '{"intent":"ACTION","plan":"search","tool_call":'
            '{"name":"general_search","args":{"text":"alpha"}},"fallback":"report failure"}'
        )


class CountingCurator:
    def __init__(self) -> None:
        self.runs = 0

    async def run(self) -> None:
        self.runs += 1


class Phase3MemoryEvolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_turns_write_patterns_and_fts_retrieves_top_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            pattern_store = PatternStore(db)
            registry = ToolRegistry([SearchTool()])
            agent = MinimalAgent(
                llm_client=Phase3LLM(),
                tool_registry=registry,
                router=ToolRouter(registry),
                goal_store=GoalStore(db),
                pattern_store=pattern_store,
                history_summary="",
            )

            for _ in range(3):
                await agent.run_turn("search alpha docs", user_id="user-1")
            await agent.drain_background_tasks()

            rows = await db.fetchall("SELECT * FROM patterns")
            matches = await pattern_store.search_patterns("alpha search", limit=3)
            await agent.run_turn("search alpha docs again", user_id="user-1")
            await agent.drain_background_tasks()

            self.assertEqual(len(rows), 3)
            self.assertEqual(len(matches), 3)
            self.assertEqual(matches[0]["pattern_type"], "success")
            self.assertEqual(matches[0]["tool_name"], "general_search")
            self.assertTrue(any("found alpha" in task_prompt for _, task_prompt in agent.llm_client.prompts))
            await db.close()

    async def test_curator_run_writes_memory_md_with_3000_char_hard_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            pattern_store = PatternStore(db)
            await pattern_store.write_pattern(
                pattern_type="success",
                trigger="search alpha docs",
                tool_name="general_search",
                args_schema='{"text":"alpha"}',
                outcome="found alpha",
                user_id="user-1",
            )
            memory_path = Path(tmp) / "soul" / "MEMORY.md"
            curator = Curator(
                pattern_store=pattern_store,
                llm_client=Phase3LLM(),
                memory_path=memory_path,
                memory_max_chars=3000,
            )

            await curator.run()
            memory = memory_path.read_text(encoding="utf-8")

            self.assertIn("general_search", memory)
            self.assertLessEqual(len(memory), 3000)
            await db.close()

    async def test_context_over_50_percent_triggers_compression_and_saves_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            context_store = ContextStore(db)
            registry = ToolRegistry([SearchTool()])
            llm = Phase3LLM()
            agent = MinimalAgent(
                llm_client=llm,
                tool_registry=registry,
                router=ToolRouter(registry),
                goal_store=GoalStore(db),
                context_store=context_store,
                context_budget_total=120,
                context_compress_threshold=0.5,
                history_summary="",
            )
            agent.conversation_history = [
                {"role": "user", "content": "old turn " + ("x" * 80)}
                for _ in range(6)
            ]

            await agent.run_turn("search alpha docs", user_id="user-1")
            await agent.drain_background_tasks()
            latest = await context_store.get_latest_summary("user-1")

            self.assertIsNotNone(latest)
            self.assertEqual(latest["summary"], "compressed middle summary")
            self.assertIn("compressed middle summary", agent.history_summary)
            self.assertTrue(any("Compress the middle conversation history" in prompt for _, prompt in llm.prompts))
            await db.close()

    async def test_agent_triggers_curator_at_completion_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            registry = ToolRegistry([SearchTool()])
            curator = CountingCurator()
            agent = MinimalAgent(
                llm_client=Phase3LLM(),
                tool_registry=registry,
                router=ToolRouter(registry),
                goal_store=GoalStore(db),
                curator=curator,
                curator_trigger_interval=2,
                history_summary="",
            )

            await agent.run_turn("search alpha docs", user_id="user-1")
            await agent.run_turn("search alpha docs", user_id="user-1")
            await agent.drain_background_tasks()

            self.assertEqual(curator.runs, 1)
            await db.close()


if __name__ == "__main__":
    unittest.main()
