from __future__ import annotations

import asyncio
import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.auth import is_authorized_user
from handlers.command import CommandHandler
from handlers.message import AgentMessageHandler
from tools.github_tools import GitHubClient
from tools.shell_tools import ShellExecutor


class FakeShell:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.work_dir = Path("/opt/luck-agent")

    async def run(self, command: str, cwd: str | None = None, **kwargs) -> dict:
        self.calls.append((command, cwd))
        return {"stdout": "", "stderr": "", "returncode": 0, "elapsed": 0, "truncated": False}


class FakeFiles:
    def __init__(self) -> None:
        self.listed: list[str] = []
        self.read: list[str] = []

    def list_dir(self, path: str = ".") -> list[dict]:
        self.listed.append(path)
        return [{"name": "a.txt", "type": "file", "size": 3, "modified": 1}]

    def read_file(self, path: str, max_chars: int = 8000) -> dict:
        self.read.append(path)
        return {"path": path, "content": "abc", "size": 3, "truncated": False}


class FakeCard:
    @staticmethod
    def file_list(files: list[dict], title: str = "VPS 文件列表") -> dict:
        return {"kind": "files", "files": files, "title": title}

    @staticmethod
    def shell_output(command: str, stdout: str, returncode: int, elapsed: float, truncated: bool = False) -> dict:
        return {"kind": "shell", "command": command, "stdout": stdout}


class SecurityHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_timeout_kills_process_group_on_posix(self) -> None:
        shell = ShellExecutor(".", timeout=1)
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.wait = AsyncMock()
        proc.pid = 12345
        proc.returncode = None

        fake_setsid = lambda: None
        with (
            patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)) as create_proc,
            patch("os.killpg", create=True) as killpg,
            patch("os.name", "posix"),
            patch("os.setsid", fake_setsid, create=True),
        ):
            result = await shell.run("sleep 60", timeout=1)

        self.assertEqual(result["returncode"], -1)
        self.assertIs(create_proc.await_args.kwargs["preexec_fn"], fake_setsid)
        killpg.assert_called_once_with(12345, getattr(signal, "SIGKILL", signal.SIGTERM))

    async def test_ls_and_cat_use_file_manager_not_shell(self) -> None:
        replies: list[dict] = []

        async def reply(chat_id: str, **kwargs) -> None:
            replies.append(kwargs)

        shell = FakeShell()
        files = FakeFiles()
        handler = CommandHandler.__new__(CommandHandler)
        handler.shell = shell
        handler.files = files
        handler.card = FakeCard()
        handler.reply = reply

        await handler._handle_ls("chat", "notes; rm -rf /")
        await handler._handle_cat("chat", "notes/a.txt; rm -rf /")

        self.assertEqual(shell.calls, [])
        self.assertEqual(files.listed, ["notes; rm -rf /"])
        self.assertEqual(files.read, ["notes/a.txt; rm -rf /"])

    async def test_git_command_quotes_work_dir_and_commit_message(self) -> None:
        replies: list[dict] = []

        async def reply(chat_id: str, **kwargs) -> None:
            replies.append(kwargs)

        shell = FakeShell()
        shell.run = AsyncMock(side_effect=[
            {"stdout": " M file.py\n", "stderr": "", "returncode": 0, "elapsed": 0, "truncated": False},
            {"stdout": "ok", "stderr": "", "returncode": 0, "elapsed": 0, "truncated": False},
        ])
        memory = SimpleNamespace(
            get_profile=lambda *_args: "/repo with space",
            set_profile=lambda *_args: None,
        )
        handler = CommandHandler.__new__(CommandHandler)
        handler.shell = shell
        handler.memory = memory
        handler.card = FakeCard()
        handler.reply = reply

        await handler._handle_git("user", "chat", 'release"; touch /tmp/pwned #')

        script = shell.run.await_args_list[1].args[0]
        self.assertIn("cd '/repo with space'", script)
        self.assertIn('git commit -m \'release"; touch /tmp/pwned #\'', script)
        self.assertNotIn('commit -m "release"; touch', script)

    async def test_blog_vps_commands_quote_paths_branch_and_commit_message(self) -> None:
        shell = FakeShell()
        github = GitHubClient("token", "owner")

        fake_cfg = SimpleNamespace(BLOG_LOCAL_PATH="/var/www/blog path")
        with patch.dict("sys.modules", {"config": SimpleNamespace(cfg=fake_cfg)}):
            result = await github._create_post_vps(
                "owner/repo",
                "owner",
                "repo",
                "main; touch /tmp/pwned",
                "content/posts/post/index.md",
                "body",
                'title"; touch /tmp/pwned #',
                shell,
            )

        self.assertNotIn("error", result)
        combined = "\n".join(command for command, _cwd in shell.calls)
        self.assertIn("'main; touch /tmp/pwned'", combined)
        self.assertIn("'Add post: title\"; touch /tmp/pwned #'", combined)
        self.assertNotIn("origin main; touch", combined)

    async def test_agent_admin_users_helper_allows_empty_and_matches_exact_user(self) -> None:
        self.assertTrue(is_authorized_user(SimpleNamespace(ADMIN_USERS=set()), "user-a"))
        self.assertTrue(is_authorized_user(SimpleNamespace(ADMIN_USERS={"user-a"}), "user-a"))
        self.assertFalse(is_authorized_user(SimpleNamespace(ADMIN_USERS={"user-a"}), "user-b"))

    async def test_ai_search_web_reuses_handler_searcher(self) -> None:
        calls: list[str] = []

        class FakeSearcher:
            async def search(self, query: str) -> dict:
                calls.append(query)
                return {"results": [], "summary": query}

        handler = AgentMessageHandler.__new__(AgentMessageHandler)
        handler.searcher = FakeSearcher()

        self.assertEqual(await handler._dispatch_tool("search_web", {"query": "one"}, "user", "chat"), {"results": [], "summary": "one"})
        self.assertEqual(await handler._dispatch_tool("search_web", {"query": "two"}, "user", "chat"), {"results": [], "summary": "two"})
        self.assertEqual(calls, ["one", "two"])


if __name__ == "__main__":
    unittest.main()
