from __future__ import annotations

import re

from interface.lark_access import LarkAccessPolicy
from interface.lark_approval import LarkApprovalManager
from interface.lark_commands import QuickCommandResult
from interface.lark_ws import LarkWebSocketInterface


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def run_turn(
        self,
        text: str,
        *,
        user_id: str = "default",
        approval_token: str | None = None,
    ) -> str:
        self.calls.append((text, user_id, approval_token))
        return "执行完成"


class FakeSender:
    def __init__(self) -> None:
        self.cards: list[dict] = []

    async def send_card(self, chat_id: str, card: dict) -> None:
        self.cards.append(card)


def _text(card: dict) -> str:
    return card["body"]["elements"][0]["content"]


def test_lark_access_policy_matches_user_or_chat() -> None:
    policy = LarkAccessPolicy.from_csv(
        user_ids="ou_allowed",
        chat_ids="oc_allowed",
    )

    assert policy.is_allowed(user_id="ou_allowed", chat_id="oc_other")
    assert policy.is_allowed(user_id="ou_other", chat_id="oc_allowed")
    assert not policy.is_allowed(user_id="ou_other", chat_id="oc_other")
    assert not LarkAccessPolicy().is_allowed(user_id="ou_any", chat_id="oc_any")


async def test_unauthorized_lark_message_is_dropped() -> None:
    agent = FakeAgent()
    sender = FakeSender()
    interface = LarkWebSocketInterface(
        agent=agent,
        sender=sender,
        access_policy=LarkAccessPolicy(allowed_chat_ids=frozenset({"oc_allowed"})),
    )

    processed = await interface.handle_message(
        {"message_id": "m1", "chat_id": "oc_other", "user_id": "ou_bad", "text": "hello"}
    )

    assert processed is False
    assert agent.calls == []
    assert sender.cards == []


def test_log_page_card_action_returns_updated_card() -> None:
    class FakeQuickCommands:
        def render_log_page(self, token: str, page: int, *, user_id: str) -> QuickCommandResult:
            return QuickCommandResult(
                f"日志第 {page} 页",
                {"schema": "2.0", "body": {"elements": []}},
            )

    interface = LarkWebSocketInterface(
        agent=FakeAgent(),
        sender=FakeSender(),
        quick_commands=FakeQuickCommands(),
        access_policy=LarkAccessPolicy(allowed_chat_ids=frozenset({"oc_allowed"})),
    )

    response = interface.handle_card_action(
        {
            "chat_id": "oc_allowed",
            "user_id": "ou_user",
            "action": {
                "tag": "button",
                "value": {"action": "vps_logs_page", "token": "page-token", "page": "2"},
            },
        }
    )

    assert response["card"]["type"] == "raw"
    assert response["toast"]["type"] == "success"


async def test_dangerous_request_requires_one_time_confirmation() -> None:
    agent = FakeAgent()
    sender = FakeSender()
    interface = LarkWebSocketInterface(
        agent=agent,
        sender=sender,
        access_policy=LarkAccessPolicy(allowed_chat_ids=frozenset({"oc_allowed"})),
        approval_manager=LarkApprovalManager(ttl_seconds=300),
    )
    base = {"chat_id": "oc_allowed", "user_id": "ou_allowed"}

    assert await interface.handle_message({**base, "message_id": "m1", "text": "重启服务"})
    assert agent.calls == []
    approval_text = _text(sender.cards[-1])
    match = re.search(r"/confirm (\S+)", approval_text)
    assert match is not None

    assert await interface.handle_message(
        {**base, "message_id": "m2", "text": f"/confirm {match.group(1)}"}
    )
    assert agent.calls[0][0:2] == ("重启服务", "ou_allowed")
    assert agent.calls[0][2]


def test_allowed_card_action_switches_target() -> None:
    selected: list[tuple[str, str]] = []

    class FakeQuickCommands:
        def select_target(self, target_id: str, user_id: str) -> QuickCommandResult:
            selected.append((target_id, user_id))
            return QuickCommandResult(f"🎯 当前目标：{target_id}")

    interface = LarkWebSocketInterface(
        agent=FakeAgent(),
        sender=FakeSender(),
        quick_commands=FakeQuickCommands(),
        access_policy=LarkAccessPolicy(allowed_chat_ids=frozenset({"oc_allowed"})),
    )

    response = interface.handle_card_action(
        {
            "chat_id": "oc_allowed",
            "user_id": "ou_user",
            "action": {
                "tag": "select_static",
                "value": {"target_id": "gcp-01"},
            },
        }
    )

    assert selected == [("gcp-01", "ou_user")]
    assert response["toast"]["type"] == "success"
    assert "gcp-01" in response["toast"]["content"]
