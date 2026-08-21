from __future__ import annotations

from runtime.contracts import RuntimeHandleResult
from interface.lark_ws import LarkWebSocketInterface


class FakeAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, text: str, *, user_id: str = "default", approval_token=None) -> str:
        self.calls += 1
        return "legacy"


class FakeRuntime:
    async def handle_message(self, **kwargs):
        return RuntimeHandleResult(
            handled=True,
            skill="langgraph",
            goal_id="goal-1234",
            intent="general",
            status="accepted",
            queue_status="pending",
            summary="已接收",
            reason="test",
        )


class FakeSender:
    def __init__(self) -> None:
        self.cards: list[tuple[str, dict]] = []

    async def send_card(self, chat_id: str, card: dict) -> None:
        self.cards.append((chat_id, card))


async def test_lark_natural_language_uses_goal_runtime() -> None:
    agent = FakeAgent()
    sender = FakeSender()
    interface = LarkWebSocketInterface(
        agent=agent,
        sender=sender,
        runtime=FakeRuntime(),
    )

    assert await interface.handle_message(
        {
            "message_id": "m-runtime",
            "user_id": "u1",
            "chat_id": "c1",
            "text": "检查服务",
        }
    )
    assert agent.calls == 0
    assert sender.cards[0][0] == "c1"
    assert sender.cards[0][1]["body"]["elements"][0]["content"] == "已接收"


async def test_lark_sends_terminal_goal_result() -> None:
    sender = FakeSender()
    interface = LarkWebSocketInterface(agent=FakeAgent(), sender=sender)

    await interface.send_goal_result(
        {
            "goal_id": "goal-123456",
            "chat_id": "c1",
            "status": "DONE",
            "result": "服务正常",
        }
    )

    assert "服务正常" in sender.cards[0][1]["body"]["elements"][0]["content"]
