from __future__ import annotations

import json
from types import SimpleNamespace

from interface.lark_api import LarkApiSender
from interface.lark_sdk import normalize_card_action_event, normalize_message_event


def _message_event(*, message_type: str = "text", content: str = '{"text":"ping"}'):
    return SimpleNamespace(
        header=SimpleNamespace(event_id="evt-1"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_user", user_id=None, union_id=None)
            ),
            message=SimpleNamespace(
                message_id="om_message",
                chat_id="oc_chat",
                message_type=message_type,
                content=content,
            ),
        ),
    )


def test_normalize_lark_message_event() -> None:
    assert normalize_message_event(_message_event()) == {
        "message_id": "om_message",
        "chat_id": "oc_chat",
        "user_id": "ou_user",
        "text": "ping",
        "event_id": "evt-1",
    }


def test_normalize_ignores_non_text_events() -> None:
    assert normalize_message_event(_message_event(message_type="image")) is None


def test_normalize_lark_card_action_event() -> None:
    event = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_user"),
            context=SimpleNamespace(open_message_id="om_card", open_chat_id="oc_chat"),
            action=SimpleNamespace(
                tag="select_static",
                value=None,
                option="gcp-01",
                name="target_select",
            ),
        )
    )

    assert normalize_card_action_event(event) == {
        "message_id": "om_card",
        "chat_id": "oc_chat",
        "user_id": "ou_user",
        "action": {
            "tag": "select_static",
            "value": None,
            "option": "gcp-01",
            "name": "target_select",
        },
    }


async def test_lark_api_sender_builds_interactive_chat_message() -> None:
    captured = {}

    class FakeMessageResource:
        def create(self, request):
            captured["request"] = request
            return SimpleNamespace(code=0, msg="ok")

    client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(message=FakeMessageResource())
        )
    )
    sender = LarkApiSender(client)

    await sender.send_card("oc_chat", {"schema": "2.0", "body": {}})

    request = captured["request"]
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == "oc_chat"
    assert request.request_body.msg_type == "interactive"
    assert json.loads(request.request_body.content)["schema"] == "2.0"
