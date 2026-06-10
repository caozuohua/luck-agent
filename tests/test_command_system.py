from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from handlers.command import AGENT_REPO, AGENT_REPO_DIR, CommandHandler


class FakeShell:
    def __init__(self) -> None:
        self.work_dir = Path("/tmp/wrong-repo")
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, command: str, cwd: str | None = None, **kwargs) -> dict:
        self.calls.append((command, cwd))
        if command == "git config --get remote.origin.url":
            return {"stdout": "git@github.com:caozuohua/luck-agent.git\n", "stderr": "", "returncode": 0}
        if command == "git pull --ff-only":
            return {"stdout": "Already up to date.\n", "stderr": "", "returncode": 0}
        if command == "sudo systemctl restart luck-agent":
            return {"stdout": "", "stderr": "", "returncode": 0}
        if command.startswith("df "):
            return {"stdout": "/dev/root 10G 4G 6G 40% /\n", "stderr": "", "returncode": 0}
        if command.startswith("free "):
            return {"stdout": "1G 400M 600M\n", "stderr": "", "returncode": 0}
        if command.startswith("ps "):
            return {"stdout": "42\n", "stderr": "", "returncode": 0}
        return {"stdout": "", "stderr": "unexpected command", "returncode": 1}

    def explain_permission_issue(self, stderr: str) -> str:
        return "permission hint"


class FakeMemory:
    db_path = "/opt/luck-agent/data/memory.sqlite"

    def stats(self) -> dict:
        return {"messages": 1, "tasks": 2, "users": 3}

    def get_recent_tasks(self, user_id: str, limit: int = 5) -> list[dict]:
        return []


class FakeBridge:
    storage = "/opt/luck-agent/uploads"


class FakeCard:
    @staticmethod
    def system_status(memory_stats: dict, task_summary: list[dict], disk=None, mem=None, procs: str = "") -> dict:
        return {"kind": "status", "memory_stats": memory_stats, "disk": disk, "mem": mem, "procs": procs}

    @staticmethod
    def health_status(details: dict) -> dict:
        raise AssertionError("/health should reuse /status")


class CommandSystemTests(unittest.IsolatedAsyncioTestCase):
    def make_handler(self, replies: list[dict]) -> CommandHandler:
        async def reply(chat_id: str, **kwargs) -> None:
            replies.append({"chat_id": chat_id, **kwargs})

        handler = CommandHandler.__new__(CommandHandler)
        handler.shell = FakeShell()
        handler.memory = FakeMemory()
        handler.bridge = FakeBridge()
        handler.card = FakeCard()
        handler.reply = reply
        handler.health = type("Health", (), {"_ws_online": True, "_ws_last_ok": 1000.0})()
        return handler

    async def test_upgrade_pulls_luck_agent_repo_from_code_directory(self) -> None:
        replies: list[dict] = []
        handler = self.make_handler(replies)

        await handler._handle_upgrade("chat")

        commands = [command for command, _cwd in handler.shell.calls]
        self.assertEqual(commands, [
            "git config --get remote.origin.url",
            "git pull --ff-only",
            "sudo systemctl restart luck-agent",
        ])
        self.assertTrue(
            all(cwd == str(AGENT_REPO_DIR) for _command, cwd in handler.shell.calls)
        )
        self.assertIn(AGENT_REPO, replies[-1]["text"])

    async def test_upgrade_stops_when_origin_is_not_luck_agent(self) -> None:
        replies: list[dict] = []
        handler = self.make_handler(replies)

        async def run_wrong_origin(command: str, cwd: str | None = None, **kwargs) -> dict:
            handler.shell.calls.append((command, cwd))
            return {"stdout": "git@github.com:caozuohua/ai-daily-newsletter.git\n", "stderr": "", "returncode": 0}

        handler.shell.run = run_wrong_origin

        await handler._handle_upgrade("chat")

        self.assertEqual([command for command, _cwd in handler.shell.calls], ["git config --get remote.origin.url"])
        self.assertIn("不是", replies[-1]["text"])
        self.assertIn(AGENT_REPO, replies[-1]["text"])

    async def test_health_reuses_status_card(self) -> None:
        replies: list[dict] = []
        handler = self.make_handler(replies)

        with patch("handlers.message.check_pkb_health", new=AsyncMock(return_value={"status": "ok", "detail": "Supabase ok"})):
            await handler._handle_health("user", "chat")

        self.assertEqual(replies[-1]["card"]["kind"], "status")
        stats = replies[-1]["card"]["memory_stats"]
        self.assertEqual(stats["db_path"], FakeMemory.db_path)
        self.assertEqual(stats["upload_dir"], FakeBridge.storage)
        self.assertEqual(stats["pkb_status"], "ok")
        self.assertEqual(stats["pkb_detail"], "Supabase ok")


if __name__ == "__main__":
    unittest.main()
