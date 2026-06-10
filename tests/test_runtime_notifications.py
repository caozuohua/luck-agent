from __future__ import annotations

import unittest

from runtime.notifications import RuntimeGoalNotifier


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def send(self, chat_id: str, **kwargs) -> None:
        self.calls.append((chat_id, kwargs))


class FakeCardBuilder:
    agent_reply_calls: list[dict] = []
    error_calls: list[tuple[str, str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.agent_reply_calls = []
        cls.error_calls = []

    @classmethod
    def agent_reply(cls, **kwargs) -> dict:
        cls.agent_reply_calls.append(kwargs)
        return {"kind": "agent_reply", **kwargs}

    @classmethod
    def error(cls, title: str, detail: str = "") -> dict:
        cls.error_calls.append((title, detail))
        return {"kind": "error", "title": title, "detail": detail}


class RuntimeGoalNotifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeCardBuilder.reset()
        self.sender = FakeSender()
        self.notifier = RuntimeGoalNotifier(
            sender=self.sender,
            card_builder=FakeCardBuilder,
        )

    async def test_done_uses_content_model_and_goal_id_without_reply_to(self) -> None:
        await self.notifier.notify({
            "goal_id": "goal-123",
            "chat_id": "chat-456",
            "status": "done",
            "artifacts": [{
                "type": "generated_content",
                "content": "final answer",
                "model": "gemini-test",
            }],
        })

        self.assertEqual(FakeCardBuilder.agent_reply_calls, [{
            "text": "final answer",
            "model": "gemini-test",
            "task_id": "goal-123",
        }])
        self.assertEqual(self.sender.calls, [(
            "chat-456",
            {
                "card": {
                    "kind": "agent_reply",
                    "text": "final answer",
                    "model": "gemini-test",
                    "task_id": "goal-123",
                },
            },
        )])
        self.assertNotIn("reply_to", self.sender.calls[0][1])

    async def test_done_uses_last_nonempty_generated_content_artifact(self) -> None:
        await self.notifier.notify({
            "goal_id": "g1",
            "chat_id": "c1",
            "status": "done",
            "artifacts": [
                {"type": "generated_content", "content": "old", "model": "m-old"},
                {"type": "log", "content": "ignored", "model": "m-log"},
                {"type": "generated_content", "content": "latest", "model": "m-new"},
                {"type": "generated_content", "content": "   ", "model": "m-empty"},
            ],
        })

        self.assertEqual(FakeCardBuilder.agent_reply_calls[0], {
            "text": "latest",
            "model": "m-new",
            "task_id": "g1",
        })

    async def test_done_without_nonempty_generated_content_raises(self) -> None:
        for artifacts in (
            [],
            [{"type": "log", "content": "trace"}],
            [{"type": "generated_content", "content": ""}],
            [{"type": "generated_content", "content": "  "}],
        ):
            with self.subTest(artifacts=artifacts):
                with self.assertRaises(ValueError):
                    await self.notifier.notify({
                        "goal_id": "g1",
                        "chat_id": "c1",
                        "status": "done",
                        "artifacts": artifacts,
                    })

        self.assertEqual(self.sender.calls, [])

    async def test_empty_chat_id_raises(self) -> None:
        for chat_id in (None, "", "   "):
            with self.subTest(chat_id=chat_id):
                with self.assertRaises(ValueError):
                    await self.notifier.notify({
                        "goal_id": "g1",
                        "chat_id": chat_id,
                        "status": "failed",
                        "error": "boom",
                    })

        self.assertEqual(self.sender.calls, [])

    async def test_failed_and_blocked_render_error_details(self) -> None:
        cases = (
            ("failed", "publish", "model unavailable"),
            ("blocked", "", "approval required"),
        )
        for status, current_step, error in cases:
            with self.subTest(status=status):
                FakeCardBuilder.reset()
                self.sender.calls.clear()

                await self.notifier.notify({
                    "goal_id": f"goal-{status}",
                    "chat_id": "chat-1",
                    "status": status,
                    "current_step": current_step,
                    "error": error,
                })

                title, detail = FakeCardBuilder.error_calls[0]
                self.assertIn(status, title)
                self.assertIn(f"Goal ID: goal-{status}", detail)
                self.assertIn(error, detail)
                if current_step:
                    self.assertIn(f"Current step: {current_step}", detail)
                else:
                    self.assertNotIn("Current step:", detail)
                self.assertEqual(self.sender.calls[0][0], "chat-1")
                self.assertNotIn("reply_to", self.sender.calls[0][1])

    async def test_cancelled_renders_error_card(self) -> None:
        await self.notifier.notify({
            "goal_id": "goal-cancelled",
            "chat_id": "chat-1",
            "status": "cancelled",
            "current_step": "draft",
            "error": "cancelled by user",
        })

        title, detail = FakeCardBuilder.error_calls[0]
        self.assertIn("cancelled", title)
        self.assertIn("Goal ID: goal-cancelled", detail)
        self.assertIn("Current step: draft", detail)
        self.assertIn("cancelled by user", detail)
        self.assertEqual(self.sender.calls[0][1]["card"]["kind"], "error")


if __name__ == "__main__":
    unittest.main()
