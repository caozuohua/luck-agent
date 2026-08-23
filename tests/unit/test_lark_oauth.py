from __future__ import annotations

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from interface.lark_oauth import LarkOAuthManager


class FakeOidc:
    def __init__(self) -> None:
        self.codes: list[str] = []

    def create(self, request):
        body = request.request_body
        token = "refreshed-token" if body.grant_type == "refresh_token" else "user-token"
        if body.grant_type == "authorization_code":
            self.codes.append(body.code)
        return SimpleNamespace(
            code=0,
            msg="ok",
            data=SimpleNamespace(
                access_token=token,
                refresh_token="refresh-token",
                expires_in=3600,
                refresh_expires_in=7200,
                scope="wiki:wiki:readonly",
            ),
        )


class FakeClient:
    def __init__(self) -> None:
        self.authen = SimpleNamespace(
            v1=SimpleNamespace(
                oidc_access_token=FakeOidc(),
                oidc_refresh_access_token=FakeOidc(),
            )
        )


class SlowOidc:
    def create(self, request):
        time.sleep(0.2)
        return SimpleNamespace(code=0, msg="ok", data=SimpleNamespace(
            access_token="user-token", expires_in=3600,
        ))


class SlowClient:
    def __init__(self) -> None:
        self.authen = SimpleNamespace(
            v1=SimpleNamespace(
                oidc_access_token=SlowOidc(),
                oidc_refresh_access_token=SlowOidc(),
            )
        )


def make_manager(**kwargs) -> LarkOAuthManager:
    return LarkOAuthManager(
        client=FakeClient(),
        app_id="cli_test",
        redirect_uri="https://agent.example.test/oauth/lark/callback",
        domain="https://open.feishu.cn",
        **kwargs,
    )


def test_authorization_url_uses_one_time_state_and_read_only_scope() -> None:
    manager = make_manager()
    url = manager.authorization_url(user_id="ou-user", chat_id="oc-chat")
    query = parse_qs(urlsplit(url).query)

    assert url.startswith("https://open.feishu.cn/open-apis/authen/v1/authorize?")
    assert query["app_id"] == ["cli_test"]
    assert query["redirect_uri"] == ["https://agent.example.test/oauth/lark/callback"]
    assert query["scope"] == ["wiki:wiki:readonly"]
    assert len(query["state"][0]) >= 32


def test_write_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="不允许"):
        make_manager(scopes="wiki:wiki")


@pytest.mark.asyncio
async def test_callback_exchanges_code_once_and_keeps_token_in_memory() -> None:
    client = FakeClient()
    manager = LarkOAuthManager(
        client=client,
        app_id="cli_test",
        redirect_uri="https://agent.example.test/oauth/lark/callback",
        domain="https://open.feishu.cn",
    )
    url = manager.authorization_url(user_id="ou-user", chat_id="oc-chat")
    state = parse_qs(urlsplit(url).query)["state"][0]

    result = await manager.handle_callback(code="one-time-code", state=state)

    assert result.ok is True
    assert manager.has_access("ou-user") is True
    assert await manager.access_token_for("ou-user") == "user-token"
    assert client.authen.v1.oidc_access_token.codes == ["one-time-code"]
    replay = await manager.handle_callback(code="one-time-code", state=state)
    assert replay.ok is False
    assert "无效或已过期" in replay.detail


@pytest.mark.asyncio
async def test_unknown_state_does_not_exchange_code() -> None:
    client = FakeClient()
    manager = LarkOAuthManager(
        client=client,
        app_id="cli_test",
        redirect_uri="https://agent.example.test/oauth/lark/callback",
        domain="https://open.feishu.cn",
    )

    result = await manager.handle_callback(code="code", state="unknown")

    assert result.ok is False
    assert client.authen.v1.oidc_access_token.codes == []


@pytest.mark.asyncio
async def test_callback_returns_when_exchange_times_out() -> None:
    manager = LarkOAuthManager(
        client=SlowClient(),
        app_id="cli_test",
        redirect_uri="https://agent.example.test/oauth/lark/callback",
        domain="https://open.feishu.cn",
        exchange_timeout_seconds=0.05,
    )
    url = manager.authorization_url(user_id="ou-user", chat_id="oc-chat")
    state = parse_qs(urlsplit(url).query)["state"][0]

    result = await manager.handle_callback(code="slow-code", state=state)

    assert result.ok is False
    assert "超时" in result.detail
