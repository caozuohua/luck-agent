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


def _client(response):
    endpoint = FakeChatEndpoint(response)
    client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(chat=endpoint)),
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
