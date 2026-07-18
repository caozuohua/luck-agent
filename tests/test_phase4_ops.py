from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from core.log import get_logger
from core.output_parser import IntentType
from core.router import ToolRouter
from interface.health import HealthService
from interface.lark_ws import LarkMessageDeduper, LarkWebSocketInterface
from memory.curator import Curator
from memory.db import Database
from memory.goal_store import GoalStatus, GoalStore
from memory.pattern_store import PatternStore
from tools.base import Tool
from tools.registry import ToolRegistry


class NamedTool(Tool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.args_schema = {}

    async def run(self, **kwargs: object):  # pragma: no cover - not used
        raise NotImplementedError


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_turn(self, text: str, *, user_id: str = "default") -> str:
        self.calls.append(f"{user_id}:{text}")
        return f"reply:{text}"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def send_card(self, chat_id: str, card: dict[str, object]) -> None:
        self.sent.append((chat_id, card))


class FakeLLM:
    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return "memory"


class Phase4OpsTests(unittest.IsolatedAsyncioTestCase):
    async def test_lark_interface_dedupes_message_for_60_seconds_and_sends_card(self) -> None:
        agent = FakeAgent()
        sender = FakeSender()
        interface = LarkWebSocketInterface(
            agent=agent,
            sender=sender,
            deduper=LarkMessageDeduper(ttl_seconds=60),
        )
        event = {
            "message_id": "m1",
            "chat_id": "c1",
            "user_id": "u1",
            "text": "hello",
        }

        first = await interface.handle_message(event)
        second = await interface.handle_message(event)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(agent.calls, ["u1:hello"])
        self.assertEqual(sender.sent[0][0], "c1")
        self.assertEqual(sender.sent[0][1]["schema"], "2.0")
        self.assertIn("reply:hello", json.dumps(sender.sent[0][1], ensure_ascii=False))

    async def test_health_service_reports_process_goals_sqlite_and_curator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            goal_store = GoalStore(db)
            done = await goal_store.create("u1", "ok")
            failed = await goal_store.create("u1", "bad")
            await goal_store.update_status(done.id, GoalStatus.DONE, result="ok")
            await goal_store.update_status(failed.id, GoalStatus.FAILED, error="bad")
            service = HealthService(db=db, goal_store=goal_store, curator_last_run_at=123.0)

            payload = await service.collect_status()

            self.assertEqual(payload["process"]["status"], "ok")
            self.assertEqual(payload["sqlite"]["connected"], True)
            self.assertEqual(payload["curator"]["last_run_at"], 123.0)
            self.assertEqual(payload["goals"]["recent_total"], 2)
            self.assertEqual(payload["goals"]["success_rate"], 0.5)
            await db.close()

    async def test_curator_periodic_task_runs_until_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "agent.db")
            await db.initialize()
            curator = Curator(
                pattern_store=PatternStore(db),
                llm_client=FakeLLM(),
                memory_path=Path(tmp) / "MEMORY.md",
                periodic_interval_seconds=0.05,
            )

            curator.start_periodic()
            await asyncio.sleep(0.08)
            await curator.stop_periodic()

            self.assertIsNotNone(curator.last_run_at)
            await db.close()

    async def test_router_watchdog_reloads_rules_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "routing_rules.yaml"
            rules.write_text(
                'rules:\n  - name: "one"\n    patterns: ["search"]\n    tools: ["one"]\nfallback_tools:\n  - "one"\n',
                encoding="utf-8",
            )
            registry = ToolRegistry([NamedTool("one"), NamedTool("two")])
            router = ToolRouter(registry, rules_path=rules, watch_interval_seconds=0.02)
            router.start_watchdog()
            rules.write_text(
                'rules:\n  - name: "two"\n    patterns: ["search"]\n    tools: ["two"]\nfallback_tools:\n  - "two"\n',
                encoding="utf-8",
            )
            await asyncio.sleep(0.08)
            await router.stop_watchdog()

            self.assertEqual(
                [tool.name for tool in router.route("search now", IntentType.ACTION)],
                ["two"],
            )

    async def test_json_lines_log_contains_required_fields(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            get_logger("phase4.test").info(
                "sample_message",
                goal_id="g1",
                duration_ms=12,
            )

        payload = json.loads(stream.getvalue().strip().splitlines()[-1])

        self.assertIn("timestamp", payload)
        self.assertEqual(payload["level"], "info")
        self.assertEqual(payload["module"], "phase4.test")
        self.assertEqual(payload["goal_id"], "g1")
        self.assertEqual(payload["message"], "sample_message")
        self.assertEqual(payload["duration_ms"], 12)

    async def test_main_runtime_initialization_sequence_is_explicit(self) -> None:
        from main import INITIALIZATION_SEQUENCE

        self.assertEqual(
            INITIALIZATION_SEQUENCE,
            [
                "load_settings",
                "initialize_sqlite",
                "recover_in_progress_goals",
                "start_lark_websocket",
                "start_health_endpoint",
                "start_curator_periodic_task",
                "register_signal_handlers",
            ],
        )


if __name__ == "__main__":
    unittest.main()
