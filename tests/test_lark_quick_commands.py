from __future__ import annotations

from interface.lark_commands import QuickCommandRouter
from interface.lark_ws import LarkWebSocketInterface
from tools.vps_status import HostStatus


class FakeHealth:
    async def collect_status(self) -> dict:
        return {
            "process": {"status": "ok"},
            "sqlite": {"connected": True},
            "goals": {"done": 3, "failed": 1},
        }


class FakeVps:
    async def collect(self) -> HostStatus:
        return HostStatus(
            hostname="aws-test",
            platform="Linux test",
            uptime_seconds=3661,
            load_1m=0.12,
            memory_total_bytes=1024 * 1024 * 1024,
            memory_available_bytes=512 * 1024 * 1024,
            disk_total_bytes=10 * 1024 * 1024 * 1024,
            disk_free_bytes=8 * 1024 * 1024 * 1024,
            collected_at=0,
        )


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_turn(self, text: str, *, user_id: str = "default") -> str:
        self.calls.append(text)
        return "LLM response"


class FakeSender:
    def __init__(self) -> None:
        self.cards: list[dict] = []

    async def send_card(self, chat_id: str, card: dict) -> None:
        self.cards.append(card)


async def test_quick_commands_return_without_llm() -> None:
    router = QuickCommandRouter(health=FakeHealth(), vps=FakeVps())

    assert await router.handle("/ping") == "🏓 pong"
    assert "SQLite：✅" in (await router.handle("/health") or "")
    assert "aws-test" in (await router.handle("/vps") or "")
    assert await router.handle("check the server") is None


async def test_lark_interface_short_circuits_quick_command() -> None:
    agent = FakeAgent()
    sender = FakeSender()
    interface = LarkWebSocketInterface(
        agent=agent,
        sender=sender,
        quick_commands=QuickCommandRouter(health=FakeHealth(), vps=FakeVps()),
    )

    processed = await interface.handle_message(
        {
            "message_id": "quick-1",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "text": "/ping",
        }
    )

    assert processed is True
    assert agent.calls == []
    assert "pong" in str(sender.cards[0])
