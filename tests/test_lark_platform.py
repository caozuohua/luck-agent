from __future__ import annotations

from types import SimpleNamespace

import pytest

from interface.lark_platform import LarkPlatformClient


class FakeChatEndpoint:
    def __init__(self, response) -> None:
        self.response = response
        self.request = None

    def get(self, request):
        self.request = request
        return self.response

    def list(self, request):
        self.request = request
        return self.response


def _client(response):
    endpoint = FakeChatEndpoint(response)
    client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                chat=endpoint,
                message=endpoint,
                chat_members=endpoint,
                chat_announcement=endpoint,
            ),
        ),
    )
    return client, endpoint


async def test_get_chat_info_maps_secret_free_metadata() -> None:
    data = SimpleNamespace(
        name="",
        chat_mode="p2p",
        chat_type="",
        description="",
        user_count="1",
        external=False,
        chat_status="normal",
    )
    client, endpoint = _client(SimpleNamespace(code=0, msg="success", data=data))

    result = await LarkPlatformClient(client).get_chat_info("oc_test")

    assert result.chat_id == "oc_test"
    assert result.chat_mode == "p2p"
    assert result.external is False
    assert endpoint.request is not None


async def test_get_chat_info_rejects_empty_id() -> None:
    client, _ = _client(SimpleNamespace(code=0, msg="success", data=None))

    with pytest.raises(ValueError, match="chat_id"):
        await LarkPlatformClient(client).get_chat_info("")


async def test_list_messages_maps_text_and_card_summaries() -> None:
    items = [
        SimpleNamespace(
            message_id="m-1",
            msg_type="text",
            create_time=1,
            sender=SimpleNamespace(sender_type="user"),
            body=SimpleNamespace(content='{"text":"hello"}'),
        ),
        SimpleNamespace(
            message_id="m-2",
            msg_type="interactive",
            create_time=2,
            sender=SimpleNamespace(sender_type="app"),
            body=SimpleNamespace(content='{"title":"a card"}'),
        ),
    ]
    data = SimpleNamespace(items=items)
    client, _ = _client(SimpleNamespace(code=0, msg="success", data=data))

    result = await LarkPlatformClient(client).list_messages("oc_test", limit=2)

    assert [item.content for item in result] == ["hello", "a card"]
    assert result[1].sender_type == "app"


async def test_list_chat_members_maps_names_without_returning_ids() -> None:
    items = [
        SimpleNamespace(name="曹佐华", member_id_type="open_id", member_id="ou_secret"),
    ]
    data = SimpleNamespace(items=items)
    client, _ = _client(SimpleNamespace(code=0, msg="success", data=data))

    result = await LarkPlatformClient(client).list_chat_members("oc_test", limit=10)

    assert result[0].name == "曹佐华"
    assert not hasattr(result[0], "member_id")


async def test_get_chat_announcement_maps_content_without_owner_id() -> None:
    data = SimpleNamespace(content="测试公告", revision="3", update_time="2026-08-22")
    client, _ = _client(SimpleNamespace(code=0, msg="success", data=data))

    result = await LarkPlatformClient(client).get_chat_announcement("oc_test")

    assert result is not None
    assert result.content == "测试公告"
    assert not hasattr(result, "owner_id")


async def test_get_chat_announcement_returns_none_when_unset() -> None:
    client, _ = _client(SimpleNamespace(code=232003, msg="not found", data=None))

    assert await LarkPlatformClient(client).get_chat_announcement("oc_test") is None
