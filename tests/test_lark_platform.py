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

    def search(self, request, option):
        self.request = request
        self.option = option
        return self.response

    def get_node(self, request, option):
        self.request = request
        self.option = option
        return self.response


class FakeBitableEndpoint:
    def __init__(self, app_response, table_response) -> None:
        self.app_response = app_response
        self.table_response = table_response
        self.app_request = None
        self.table_request = None
        self.option = None

    def get(self, request, option):
        self.app_request = request
        self.option = option
        return self.app_response

    def list(self, request, option):
        self.table_request = request
        self.option = option
        return self.table_response

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


def _wiki_client(response):
    endpoint = FakeChatEndpoint(response)
    client = SimpleNamespace(
        wiki=SimpleNamespace(
            v1=SimpleNamespace(node=endpoint),
            v2=SimpleNamespace(space=endpoint),
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


async def test_search_wiki_uses_user_token_and_hides_node_ids() -> None:
    item = SimpleNamespace(
        node_id="node-secret",
        title="部署手册",
        url="https://open.larksuite.com/wiki/abc",
        domain="bitable",
        obj_type=11,
    )
    client, endpoint = _wiki_client(
        SimpleNamespace(
            code=0,
            msg="success",
            data=SimpleNamespace(items=[item], has_more=False),
        )
    )

    result = await LarkPlatformClient(client).search_wiki(
        "部署",
        user_access_token="user-token",
    )

    assert result.items[0].title == "部署手册"
    assert result.items[0].url.endswith("/abc")
    assert not hasattr(result.items[0], "node_id")
    assert endpoint.request.body.query == "部署"
    assert endpoint.option.user_access_token == "user-token"


async def test_get_wiki_node_accepts_url_and_returns_safe_metadata() -> None:
    node = SimpleNamespace(
        node_token="node-secret",
        obj_token="obj-secret",
        title="部署手册",
        url="https://open.larksuite.com/wiki/abc",
        obj_type="bitable",
        node_type="origin",
        has_child=True,
        obj_edit_time=1720000000000,
    )
    client, endpoint = _wiki_client(
        SimpleNamespace(
            code=0,
            msg="success",
            data=SimpleNamespace(node=node),
        )
    )

    result = await LarkPlatformClient(client).get_wiki_node(
        "https://open.larksuite.com/wiki/abc?from=search",
        user_access_token="user-token",
    )

    assert result.title == "部署手册"
    assert result.obj_type == "bitable"
    assert result.has_child is True
    assert not hasattr(result, "node_token")
    assert endpoint.request.token == "abc"
    assert endpoint.option.user_access_token == "user-token"


async def test_get_wiki_node_rejects_malformed_url() -> None:
    client, _ = _wiki_client(SimpleNamespace(code=0, msg="success", data=None))

    with pytest.raises(ValueError, match="Wiki"):
        await LarkPlatformClient(client).get_wiki_node(
            "https://open.larksuite.com/docs/abc",
            user_access_token="user-token",
        )


async def test_summarize_wiki_bitable_returns_table_names_without_ids() -> None:
    node_endpoint = FakeChatEndpoint(
        SimpleNamespace(
            code=0,
            msg="success",
            data=SimpleNamespace(
                node=SimpleNamespace(
                    title="QPC个人知识库",
                    url="https://open.larksuite.com/wiki/abc",
                    obj_type="bitable",
                    obj_token="app-secret",
                )
            ),
        )
    )
    app_endpoint = FakeBitableEndpoint(
        SimpleNamespace(
            code=0,
            msg="success",
            data=SimpleNamespace(app=SimpleNamespace(name="QPC个人知识库")),
        ),
        SimpleNamespace(
            code=0,
            msg="success",
            data=SimpleNamespace(
                items=[
                    SimpleNamespace(name="Roadmap", table_id="tbl-secret"),
                    SimpleNamespace(name="运维记录", table_id="tbl-secret-2"),
                ],
                has_more=False,
            ),
        ),
    )
    client = SimpleNamespace(
        wiki=SimpleNamespace(v2=SimpleNamespace(space=node_endpoint)),
        bitable=SimpleNamespace(
            v1=SimpleNamespace(app=app_endpoint, app_table=app_endpoint)
        ),
    )

    result = await LarkPlatformClient(client).summarize_wiki_bitable(
        "https://open.larksuite.com/wiki/abc",
        user_access_token="user-token",
    )

    assert result.title == "QPC个人知识库"
    assert result.tables == ("Roadmap", "运维记录")
    assert "tbl-secret" not in str(result)
    assert app_endpoint.app_request.app_token == "app-secret"
    assert app_endpoint.table_request.app_token == "app-secret"
    assert app_endpoint.option.user_access_token == "user-token"
