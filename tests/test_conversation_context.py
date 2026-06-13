from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent import AgentApp
from core.memory import Memory
from handlers.message import AgentMessageHandler


class FakeCard:
    @staticmethod
    def agent_reply(**kwargs) -> dict:
        return kwargs

    @staticmethod
    def error(title: str, detail: str) -> dict:
        return {"title": title, "detail": detail}


class ControlledRouter:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    def build_system_prompt(self, *args, **kwargs) -> str:
        return "system"

    async def chat(self, *, messages: list[dict], **kwargs) -> dict:
        self.calls.append([dict(message) for message in messages])
        if len(self.calls) == 1:
            self.first_started.set()
            await self.release_first.wait()
            text = "第一条回复"
        else:
            text = "第二条回复"
        return {
            "text": text,
            "tool_calls": [],
            "model": "test-model",
            "tokens": 1,
        }


def build_handler(memory: Memory, router) -> AgentMessageHandler:
    handler = AgentMessageHandler.__new__(AgentMessageHandler)
    handler._user_locks = {}
    handler.cfg = SimpleNamespace(
        MODEL_PRO="pro",
        MODEL_FLASH="flash",
        pick_model=lambda text: "test-model",
    )
    handler.memory = memory
    handler.router = router
    handler.all_tools = []
    handler.card = FakeCard
    handler.reply = AsyncMock()
    return handler


class ConversationContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_messages_are_serialized_and_include_prior_turn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "memory.db"))
            router = ControlledRouter()
            handler = build_handler(memory, router)

            with patch("handlers.message.intent_route") as route:
                route.return_value = SimpleNamespace(
                    intent=SimpleNamespace(value="general"),
                    confidence=1.0,
                    tool_names=[],
                    model_hint="",
                    prompt_hint="",
                )
                first = asyncio.create_task(
                    handler.handle("user-1", "chat-1", "message-1", "第一条")
                )
                await asyncio.wait_for(router.first_started.wait(), timeout=1)
                second = asyncio.create_task(
                    handler.handle("user-1", "chat-1", "message-2", "第二条")
                )
                await asyncio.sleep(0.05)

                self.assertEqual(len(router.calls), 1)
                self.assertEqual(
                    router.calls[0],
                    [{"role": "user", "content": "第一条", "model": ""}],
                )

                router.release_first.set()
                await asyncio.gather(first, second)

            self.assertEqual(
                [(item["role"], item["content"]) for item in router.calls[1]],
                [
                    ("user", "第一条"),
                    ("assistant", "第一条回复"),
                    ("user", "第二条"),
                ],
            )
            memory._local.conn.close()

    async def test_model_failure_persists_balancing_assistant_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "memory.db"))
            router = SimpleNamespace(
                build_system_prompt=lambda *args, **kwargs: "system",
                chat=AsyncMock(side_effect=RuntimeError("provider unavailable")),
            )
            handler = build_handler(memory, router)

            with patch("handlers.message.intent_route") as route:
                route.return_value = SimpleNamespace(
                    intent=SimpleNamespace(value="general"),
                    confidence=1.0,
                    tool_names=[],
                    model_hint="",
                    prompt_hint="",
                )
                await handler.handle(
                    "user-1", "chat-1", "message-1", "会失败的消息"
                )

            history = memory.get_history("user-1")
            self.assertEqual([item["role"] for item in history], ["user", "assistant"])
            self.assertIn("模型调用失败", history[-1]["content"])
            memory._local.conn.close()

    async def test_goal_acceptance_is_persisted_as_complete_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = Memory(str(Path(temp_dir) / "memory.db"))
            app = AgentApp.__new__(AgentApp)
            app.cfg = SimpleNamespace(
                ADMIN_USERS={"user-1"},
                MODEL_PRO="",
                MODEL_FLASH="",
                MODEL_LITE="",
            )
            app._health = SimpleNamespace(mark_ws_ok=lambda: None)
            app._memory = memory
            app._sender = SimpleNamespace(send=AsyncMock())
            app._cmd_handler = SimpleNamespace(
                is_command=lambda text: False,
                handle=AsyncMock(return_value=False),
            )
            app._runtime_manager = SimpleNamespace(
                handle_message=AsyncMock(return_value=SimpleNamespace(
                    handled=True,
                    goal_id="goal-123456",
                    summary="正在生成博客选题",
                )),
                mark_accepted=lambda goal_id: None,
            )

            await app._on_message({
                "event": {
                    "message": {
                        "chat_id": "chat-1",
                        "message_id": "message-1",
                        "message_type": "text",
                        "chat_type": "p2p",
                        "content": '{"text":"帮我生成博客选题"}',
                    },
                    "sender": {"sender_id": {"open_id": "user-1"}},
                },
            })

            history = memory.get_history("user-1")
            self.assertEqual([item["role"] for item in history], ["user", "assistant"])
            self.assertEqual(history[0]["content"], "帮我生成博客选题")
            self.assertIn("任务已接受", history[1]["content"])
            memory._local.conn.close()


if __name__ == "__main__":
    unittest.main()
