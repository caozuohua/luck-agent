from __future__ import annotations

import re

from interface.lark_access import LarkAccessPolicy
from interface.lark_approval import LarkApprovalManager
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
