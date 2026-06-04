from __future__ import annotations

import unittest

from handlers.command import CommandHandler


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def trigger_workflow(self, repo: str, workflow_id: str) -> dict:
        self.calls.append((repo, workflow_id))
        return {"triggered": True, "workflow": workflow_id}


class CommandDeployTests(unittest.IsolatedAsyncioTestCase):
    async def test_deploy_command_uses_hugo_workflow(self) -> None:
        replies: list[dict] = []

        async def reply(chat_id: str, **kwargs) -> None:
            replies.append({"chat_id": chat_id, **kwargs})

        github = FakeGitHub()
        handler = CommandHandler.__new__(CommandHandler)
        handler.github = github
        handler.hugo_repo = ""
        handler.reply = reply

        await handler._handle_deploy("user", "chat", "owner/repo")

        self.assertEqual(github.calls, [("owner/repo", "deploy-hugo.yml")])
        self.assertIn("deploy-hugo.yml", replies[0]["text"])


if __name__ == "__main__":
    unittest.main()
