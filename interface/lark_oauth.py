from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import lark_oapi as lark
from lark_oapi.api.authen.v1 import (
    CreateOidcAccessTokenRequest,
    CreateOidcAccessTokenRequestBody,
    CreateOidcRefreshAccessTokenRequest,
    CreateOidcRefreshAccessTokenRequestBody,
)


READ_ONLY_SCOPE_ALLOWLIST = {
    "docx:document:readonly",
    "drive:drive:readonly",
    "offline_access",
    "space:document:retrieve",
    "wiki:wiki:readonly",
}


class LarkOAuthError(RuntimeError):
    """A user-facing OAuth failure without credentials or token values."""


@dataclass(frozen=True)
class PendingLarkAuthorization:
    state: str
    user_id: str
    chat_id: str
    created_at: float
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class LarkUserToken:
    """An in-memory user token; repr deliberately never exposes credentials."""

    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    scope: str = ""

    def __repr__(self) -> str:  # pragma: no cover - defensive logging guard
        return "LarkUserToken(<redacted>)"


@dataclass(frozen=True)
class LarkOAuthCallback:
    ok: bool
    detail: str
    user_id: str = ""
    chat_id: str = ""


class LarkOAuthManager:
    """Small, read-only User OAuth boundary for Lark Wiki/Docs access.

    State and tokens are intentionally process-local. A service restart drops
    them and requires a fresh user authorization, which keeps the first
    rollout free of a persistent secret store.
    """

    def __init__(
        self,
        *,
        client: lark.Client,
        app_id: str,
        redirect_uri: str,
        domain: str,
        scopes: str = "wiki:wiki:readonly",
        state_ttl_seconds: float = 600.0,
        token_skew_seconds: float = 60.0,
        max_states: int = 128,
    ) -> None:
        self.client = client
        self.app_id = str(app_id or "").strip()
        self.redirect_uri = str(redirect_uri or "").strip()
        self.domain = str(domain or "").rstrip("/")
        self.scopes = _normalize_scopes(scopes)
        self.state_ttl_seconds = max(60.0, float(state_ttl_seconds))
        self.token_skew_seconds = max(0.0, float(token_skew_seconds))
        self.max_states = max(8, int(max_states))
        self._states: dict[str, PendingLarkAuthorization] = {}
        self._tokens: dict[str, LarkUserToken] = {}
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.redirect_uri and self.domain and self.scopes)

    @property
    def scope_text(self) -> str:
        return " ".join(self.scopes)

    def authorization_url(self, *, user_id: str, chat_id: str) -> str:
        if not self.configured:
            raise LarkOAuthError("尚未配置 LARK_OAUTH_REDIRECT_URI")
        normalized_user = str(user_id or "").strip()
        normalized_chat = str(chat_id or "").strip()
        if not normalized_user or not normalized_chat:
            raise LarkOAuthError("缺少当前 Lark 用户或会话信息")
        self._prune_states()
        state = secrets.token_urlsafe(32)
        self._states[state] = PendingLarkAuthorization(
            state=state,
            user_id=normalized_user,
            chat_id=normalized_chat,
            created_at=time.time(),
            scopes=self.scopes,
        )
        while len(self._states) > self.max_states:
            oldest = min(self._states.values(), key=lambda item: item.created_at)
            self._states.pop(oldest.state, None)
        query = urlencode(
            {
                "app_id": self.app_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "scope": self.scope_text,
            }
        )
        return f"{self.domain}/open-apis/authen/v1/authorize?{query}"

    def has_access(self, user_id: str) -> bool:
        token = self._tokens.get(str(user_id or "").strip())
        return token is not None and token.expires_at > time.time()

    async def access_token_for(self, user_id: str) -> str | None:
        normalized_user = str(user_id or "").strip()
        if not normalized_user:
            return None
        async with self._token_lock:
            token = self._tokens.get(normalized_user)
            if token is None:
                return None
            if token.expires_at > time.time() + self.token_skew_seconds:
                return token.access_token
            if not token.refresh_token or token.refresh_expires_at <= time.time():
                self._tokens.pop(normalized_user, None)
                return None
            refreshed = await asyncio.to_thread(self._refresh_token, token.refresh_token)
            self._tokens[normalized_user] = refreshed
            return refreshed.access_token

    async def handle_callback(
        self,
        *,
        code: str = "",
        state: str = "",
        error: str = "",
    ) -> LarkOAuthCallback:
        normalized_state = str(state or "").strip()
        pending = self._consume_state(normalized_state)
        if pending is None:
            return LarkOAuthCallback(False, "授权状态无效或已过期")
        if error:
            return LarkOAuthCallback(False, "用户取消或拒绝了授权")
        normalized_code = str(code or "").strip()
        if not normalized_code:
            return LarkOAuthCallback(False, "回调缺少授权码")
        try:
            token = await asyncio.to_thread(self._exchange_code, normalized_code)
        except Exception as exc:
            detail = str(exc).strip() or "授权码交换失败"
            return LarkOAuthCallback(False, detail, pending.user_id, pending.chat_id)
        self._tokens[pending.user_id] = token
        return LarkOAuthCallback(
            True,
            "只读授权已完成，可返回 Lark 继续查询 Wiki/文档",
            pending.user_id,
            pending.chat_id,
        )

    def _consume_state(self, state: str) -> PendingLarkAuthorization | None:
        if not state:
            return None
        pending = self._states.pop(state, None)
        if pending is None or time.time() - pending.created_at > self.state_ttl_seconds:
            return None
        return pending

    def _prune_states(self) -> None:
        now = time.time()
        expired = [
            state
            for state, pending in self._states.items()
            if now - pending.created_at > self.state_ttl_seconds
        ]
        for state in expired:
            self._states.pop(state, None)

    def _exchange_code(self, code: str) -> LarkUserToken:
        body = (
            CreateOidcAccessTokenRequestBody.builder()
            .grant_type("authorization_code")
            .code(code)
            .build()
        )
        request = CreateOidcAccessTokenRequest.builder().request_body(body).build()
        response = self.client.authen.v1.oidc_access_token.create(request)
        if response.code != 0 or response.data is None:
            raise LarkOAuthError(f"Lark OAuth 授权码交换失败：{response.code} {response.msg}")
        return _token_from_response(response.data)

    def _refresh_token(self, refresh_token: str) -> LarkUserToken:
        body = (
            CreateOidcRefreshAccessTokenRequestBody.builder()
            .grant_type("refresh_token")
            .refresh_token(refresh_token)
            .build()
        )
        request = (
            CreateOidcRefreshAccessTokenRequest.builder()
            .request_body(body)
            .build()
        )
        response = self.client.authen.v1.oidc_refresh_access_token.create(request)
        if response.code != 0 or response.data is None:
            raise LarkOAuthError(f"Lark OAuth 令牌刷新失败：{response.code} {response.msg}")
        return _token_from_response(response.data)


def _normalize_scopes(raw: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(str(raw or "").split()))
    invalid = [scope for scope in values if scope not in READ_ONLY_SCOPE_ALLOWLIST]
    if invalid:
        raise ValueError(f"只读 OAuth 不允许的 scope：{', '.join(invalid)}")
    return values


def _token_from_response(data: Any) -> LarkUserToken:
    now = time.time()
    expires_in = max(0, int(getattr(data, "expires_in", 0) or 0))
    refresh_expires_in = max(0, int(getattr(data, "refresh_expires_in", 0) or 0))
    access_token = str(getattr(data, "access_token", "") or "")
    if not access_token:
        raise LarkOAuthError("Lark OAuth 返回了空 access token")
    return LarkUserToken(
        access_token=access_token,
        refresh_token=str(getattr(data, "refresh_token", "") or ""),
        expires_at=now + expires_in,
        refresh_expires_at=now + refresh_expires_in,
        scope=str(getattr(data, "scope", "") or ""),
    )
