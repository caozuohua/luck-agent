from __future__ import annotations

from core.targets import VpsTarget, VpsTargetRegistry
from interface.lark_commands import QuickCommandRouter
from interface.lark_ws import LarkWebSocketInterface
from tools.mem0_client import Mem0Health, Mem0SmokeResult
from tools.vps_status import HostStatus
from tools.vps_sysops import VpsSysopsResult


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


class FakeSysops:
    async def run(self, operation: str) -> VpsSysopsResult:
        return VpsSysopsResult(operation=operation, ok=True, output=f"checked {operation}")


class FakeMem0:
    async def health(self) -> Mem0Health:
        return Mem0Health(ok=True, latency_ms=4, detail="test")

    async def smoke(self) -> Mem0SmokeResult:
        return Mem0SmokeResult(
            ok=True,
            marker="test-marker",
            added=1,
            found=1,
            deleted=1,
            cleanup_confirmed=True,
        )

    async def search(self, query: str) -> list[dict]:
        return [{"id": "memory-1", "memory": f"remember {query}", "score": 0.9}]


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
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        sysops=FakeSysops(),
        mem0=FakeMem0(),
    )

    assert await router.handle("/ping") == "🏓 pong"
    assert "SQLite：✅" in (await router.handle("/health") or "")
    assert "aws-test" in (await router.handle("/vps") or "")
    assert "checked resources" in (await router.handle("/vps resources") or "")
    assert "延迟：4 ms" in (await router.handle("/mem0 status") or "")
    assert "临时标识" in (await router.handle("/mem0 smoke") or "")
    assert "remember database" in (await router.handle("/mem0 search database") or "")
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


async def test_target_commands_return_card_and_keep_user_selection() -> None:
    default = VpsTarget(provider="aws", target_id="aws-01")
    targets = VpsTargetRegistry.from_csv(
        "gcp-01|gcp||us-west1|staging",
        default_target=default,
    )
    router = QuickCommandRouter(
        health=FakeHealth(),
        vps=FakeVps(),
        targets=targets,
    )

    result = await router.handle("/targets", user_id="alice")
    assert result is not None
    assert result.text == "🎯 当前目标：AWS / aws-01"
    assert result.card is not None
    assert result.card["body"]["elements"][1]["options"]

    selected = await router.handle("/target gcp-01", user_id="alice")
    assert selected.text == "🎯 当前目标：GCP / gcp-01 / us-west1"
    assert targets.current("alice").label == "gcp-01"
    assert targets.current("bob").label == "aws-01"
