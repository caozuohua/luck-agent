from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import GetAppRequest, ListAppTableRequest
from lark_oapi.api.wiki.v1 import SearchNodeRequest, SearchNodeRequestBody
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest
from lark_oapi.core.model import RequestOption
from lark_oapi.api.im.v1 import (
    GetChatAnnouncementRequest,
    GetChatMembersRequest,
    GetChatRequest,
    ListMessageRequest,
)


@dataclass(frozen=True)
class LarkChatInfo:
    """Secret-free metadata for the chat that issued a command."""

    chat_id: str
    name: str = ""
    chat_mode: str = ""
    chat_type: str = ""
    description: str = ""
    user_count: str = ""
    external: bool | None = None
    chat_status: str = ""


@dataclass(frozen=True)
class LarkMessageInfo:
    """Safe summary of one message in the current chat."""

    message_id: str
    msg_type: str = ""
    sender_type: str = ""
    content: str = ""
    create_time: int = 0


@dataclass(frozen=True)
class LarkChatMemberInfo:
    """Privacy-safe summary of one member in the current chat."""

    name: str = ""
    member_id_type: str = ""


@dataclass(frozen=True)
class LarkChatAnnouncement:
    """Privacy-safe current announcement content without owner IDs."""

    content: str = ""
    revision: str = ""
    update_time: str = ""


@dataclass(frozen=True)
class LarkWikiNode:
    """Safe, user-visible Wiki search result without internal node IDs."""

    title: str = ""
    url: str = ""
    domain: str = ""
    obj_type: int | None = None


@dataclass(frozen=True)
class LarkWikiSearchResult:
    items: tuple[LarkWikiNode, ...] = ()
    has_more: bool = False


@dataclass(frozen=True)
class LarkWikiNodeDetail:
    """Safe metadata for a Wiki node; internal tokens are intentionally omitted."""

    title: str = ""
    url: str = ""
    obj_type: str = ""
    node_type: str = ""
    has_child: bool | None = None
    edited_at: int = 0


@dataclass(frozen=True)
class LarkBitableSummary:
    """Safe metadata for a Wiki node backed by a Bitable app."""

    title: str = ""
    url: str = ""
    tables: tuple[str, ...] = ()
    has_more: bool = False


class LarkPlatformClient:
    """Read-only Lark platform queries used by deterministic commands."""

    def __init__(self, client: lark.Client) -> None:
        self.client = client

    async def get_chat_info(self, chat_id: str) -> LarkChatInfo:
        normalized = str(chat_id or "").strip()
        if not normalized:
            raise ValueError("chat_id is required")
        request = GetChatRequest.builder().chat_id(normalized).build()
        response = await asyncio.to_thread(self.client.im.v1.chat.get, request)
        if response.code != 0 or response.data is None:
            raise RuntimeError(f"Lark chat query failed: {response.code} {response.msg}")
        data = response.data
        return LarkChatInfo(
            chat_id=normalized,
            name=str(getattr(data, "name", "") or ""),
            chat_mode=str(getattr(data, "chat_mode", "") or ""),
            chat_type=str(getattr(data, "chat_type", "") or ""),
            description=str(getattr(data, "description", "") or ""),
            user_count=str(getattr(data, "user_count", "") or ""),
            external=getattr(data, "external", None),
            chat_status=str(getattr(data, "chat_status", "") or ""),
        )

    async def list_messages(self, chat_id: str, *, limit: int = 5) -> tuple[LarkMessageInfo, ...]:
        normalized = str(chat_id or "").strip()
        if not normalized:
            raise ValueError("chat_id is required")
        page_size = max(1, min(int(limit), 10))
        request = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(normalized)
            .page_size(page_size)
            .sort_type("ByCreateTimeDesc")
            .with_sender_name(True)
            .build()
        )
        response = await asyncio.to_thread(self.client.im.v1.message.list, request)
        if response.code != 0 or response.data is None:
            raise RuntimeError(f"Lark message query failed: {response.code} {response.msg}")
        result: list[LarkMessageInfo] = []
        for item in response.data.items or ():
            body = getattr(item, "body", None)
            raw_content = str(getattr(body, "content", "") or "")
            result.append(
                LarkMessageInfo(
                    message_id=str(getattr(item, "message_id", "") or ""),
                    msg_type=str(getattr(item, "msg_type", "") or ""),
                    sender_type=str(getattr(getattr(item, "sender", None), "sender_type", "") or ""),
                    content=_message_content(raw_content),
                    create_time=int(getattr(item, "create_time", 0) or 0),
                )
            )
        return tuple(result)

    async def list_chat_members(
        self,
        chat_id: str,
        *,
        limit: int = 10,
    ) -> tuple[LarkChatMemberInfo, ...]:
        normalized = str(chat_id or "").strip()
        if not normalized:
            raise ValueError("chat_id is required")
        page_size = max(1, min(int(limit), 10))
        request = (
            GetChatMembersRequest.builder()
            .chat_id(normalized)
            .member_id_type("open_id")
            .page_size(page_size)
            .build()
        )
        response = await asyncio.to_thread(self.client.im.v1.chat_members.get, request)
        if response.code != 0 or response.data is None:
            raise RuntimeError(f"Lark chat members query failed: {response.code} {response.msg}")
        return tuple(
            LarkChatMemberInfo(
                name=str(getattr(item, "name", "") or "")[:80],
                member_id_type=str(getattr(item, "member_id_type", "") or ""),
            )
            for item in (response.data.items or ())
        )

    async def get_chat_announcement(self, chat_id: str) -> LarkChatAnnouncement | None:
        normalized = str(chat_id or "").strip()
        if not normalized:
            raise ValueError("chat_id is required")
        request = GetChatAnnouncementRequest.builder().chat_id(normalized).build()
        response = await asyncio.to_thread(self.client.im.v1.chat_announcement.get, request)
        if response.code == 232003 and response.data is None:
            return None
        if response.code != 0 or response.data is None:
            raise RuntimeError(
                f"Lark chat announcement query failed: {response.code} {response.msg}"
            )
        data = response.data
        return LarkChatAnnouncement(
            content=str(getattr(data, "content", "") or "")[:2000],
            revision=str(getattr(data, "revision", "") or ""),
            update_time=str(getattr(data, "update_time", "") or ""),
        )

    async def search_wiki(
        self,
        query: str,
        *,
        user_access_token: str,
        limit: int = 5,
    ) -> LarkWikiSearchResult:
        normalized_query = str(query or "").strip()
        normalized_token = str(user_access_token or "").strip()
        if not normalized_query:
            raise ValueError("query is required")
        if not normalized_token:
            raise ValueError("user_access_token is required")
        page_size = max(1, min(int(limit), 10))
        body = SearchNodeRequestBody.builder().query(normalized_query[:200]).build()
        request = (
            SearchNodeRequest.builder()
            .page_size(page_size)
            .request_body(body)
            .build()
        )
        option = RequestOption.builder().user_access_token(normalized_token).build()
        try:
            response = await asyncio.to_thread(
                self.client.wiki.v1.node.search,
                request,
                option,
            )
        except json.JSONDecodeError:
            # lark-oapi 1.7.x attempts to deserialize every JSON content-type
            # response before returning it. Some Lark Wiki gateways return an
            # empty/non-JSON body on an otherwise useful response; use the
            # same user token through the documented HTTP endpoint as a
            # compatibility fallback.
            return await self._search_wiki_http(
                normalized_query,
                user_access_token=normalized_token,
                limit=page_size,
            )
        if response.code != 0 or response.data is None:
            raise RuntimeError(f"Lark Wiki search failed: {response.code} {response.msg}")
        return LarkWikiSearchResult(
            items=tuple(
                LarkWikiNode(
                    title=str(getattr(item, "title", "") or "")[:200],
                    url=str(getattr(item, "url", "") or "")[:1000],
                    domain=str(getattr(item, "domain", "") or "")[:40],
                    obj_type=getattr(item, "obj_type", None),
                )
                for item in (response.data.items or ())
            ),
            has_more=bool(getattr(response.data, "has_more", False)),
        )

    async def _search_wiki_http(
        self,
        query: str,
        *,
        user_access_token: str,
        limit: int,
    ) -> LarkWikiSearchResult:
        config = getattr(self.client, "config", None)
        domain = str(getattr(config, "domain", "") or "").rstrip("/")
        if not domain:
            raise RuntimeError("Lark Wiki search failed: API domain is unavailable")
        url = f"{domain}/open-apis/wiki/v2/nodes/search"
        headers = {
            "Authorization": f"Bearer {user_access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=15.0) as transport:
            response = await transport.post(
                url,
                params={"page_size": limit},
                headers=headers,
                json={"query": query},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Lark Wiki search failed: HTTP {response.status_code}, invalid response"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"Lark Wiki search failed: HTTP {response.status_code} "
                f"{str(payload.get('msg') or '')[:160]}"
            )
        response_code = payload.get("code")
        if response_code is None or int(response_code) != 0:
            raise RuntimeError(
                f"Lark Wiki search failed: {response_code} "
                f"{str(payload.get('msg') or '')[:160]}"
            )
        data = payload.get("data") or {}
        items = data.get("items") or []
        return LarkWikiSearchResult(
            items=tuple(
                LarkWikiNode(
                    title=str(item.get("title") or "")[:200],
                    url=str(item.get("url") or "")[:1000],
                    domain=str(item.get("domain") or "")[:40],
                    obj_type=item.get("obj_type") if isinstance(item.get("obj_type"), int) else None,
                )
                for item in items
                if isinstance(item, dict)
            ),
            has_more=bool(data.get("has_more", False)),
        )

    async def get_wiki_node(
        self,
        reference: str,
        *,
        user_access_token: str,
    ) -> LarkWikiNodeDetail:
        """Read Wiki node metadata from a URL or node token using User OAuth."""
        node = await self._fetch_wiki_node(
            reference,
            user_access_token=user_access_token,
        )
        return _wiki_node_detail(node)

    async def summarize_wiki_bitable(
        self,
        reference: str,
        *,
        user_access_token: str,
        limit: int = 10,
    ) -> LarkBitableSummary:
        """Read a Wiki Bitable app name and table names without returning IDs or records."""
        node = await self._fetch_wiki_node(
            reference,
            user_access_token=user_access_token,
        )
        obj_type = str(_node_value(node, "obj_type", "") or "").lower()
        if obj_type not in {"bitable", "11"}:
            raise ValueError("当前 Wiki 节点不是多维表格，暂只支持 Bitable 摘要")
        app_token = str(_node_value(node, "obj_token", "") or "").strip()
        if not app_token:
            raise RuntimeError("Lark Bitable 摘要失败：节点缺少 app token")
        normalized_token = str(user_access_token or "").strip()
        if not normalized_token:
            raise ValueError("user_access_token is required")
        page_size = max(1, min(int(limit), 20))
        app_endpoint = getattr(
            getattr(getattr(self.client, "bitable", None), "v1", None),
            "app",
            None,
        )
        table_endpoint = getattr(
            getattr(getattr(self.client, "bitable", None), "v1", None),
            "app_table",
            None,
        )
        if app_endpoint is None or table_endpoint is None:
            raise RuntimeError("Lark Bitable 摘要失败：SDK 未提供只读接口")
        option = RequestOption.builder().user_access_token(normalized_token).build()
        app_request = GetAppRequest.builder().app_token(app_token).build()
        table_request = (
            ListAppTableRequest.builder()
            .app_token(app_token)
            .page_size(page_size)
            .build()
        )
        app_response = await asyncio.to_thread(app_endpoint.get, app_request, option)
        if app_response.code != 0 or app_response.data is None or app_response.data.app is None:
            raise RuntimeError(f"Lark Bitable app query failed: {app_response.code} {app_response.msg}")
        table_response = await asyncio.to_thread(table_endpoint.list, table_request, option)
        if table_response.code != 0 or table_response.data is None:
            raise RuntimeError(
                f"Lark Bitable table query failed: {table_response.code} {table_response.msg}"
            )
        return LarkBitableSummary(
            title=str(
                getattr(app_response.data.app, "name", "")
                or _node_value(node, "title", "")
                or ""
            )[:200],
            url=str(_node_value(node, "url", "") or "")[:1000],
            tables=tuple(
                str(getattr(item, "name", "") or "")[:160]
                for item in (table_response.data.items or ())
                if str(getattr(item, "name", "") or "").strip()
            ),
            has_more=bool(getattr(table_response.data, "has_more", False)),
        )

    async def _fetch_wiki_node(
        self,
        reference: str,
        *,
        user_access_token: str,
    ) -> object:
        token, obj_type = _parse_wiki_reference(reference)
        normalized_token = str(user_access_token or "").strip()
        if not normalized_token:
            raise ValueError("user_access_token is required")
        request_builder = GetNodeSpaceRequest.builder().token(token)
        if obj_type:
            request_builder.obj_type(obj_type)
        request = request_builder.build()
        option = RequestOption.builder().user_access_token(normalized_token).build()
        endpoint = getattr(
            getattr(getattr(self.client, "wiki", None), "v2", None),
            "space",
            None,
        )
        try:
            if endpoint is None or not callable(getattr(endpoint, "get_node", None)):
                return await self._get_wiki_node_http(
                    token,
                    obj_type=obj_type,
                    user_access_token=normalized_token,
                )
            response = await asyncio.to_thread(endpoint.get_node, request, option)
        except json.JSONDecodeError:
            return await self._get_wiki_node_http(
                token,
                obj_type=obj_type,
                user_access_token=normalized_token,
            )
        if response.code != 0 or response.data is None or response.data.node is None:
            raise RuntimeError(f"Lark Wiki node query failed: {response.code} {response.msg}")
        return response.data.node

    async def _get_wiki_node_http(
        self,
        token: str,
        *,
        obj_type: str,
        user_access_token: str,
    ) -> object:
        config = getattr(self.client, "config", None)
        domain = str(getattr(config, "domain", "") or "").rstrip("/")
        if not domain:
            raise RuntimeError("Lark Wiki node query failed: API domain is unavailable")
        url = f"{domain}/open-apis/wiki/v2/spaces/get_node"
        params = {"token": token}
        if obj_type:
            params["obj_type"] = obj_type
        headers = {"Authorization": f"Bearer {user_access_token}"}
        async with httpx.AsyncClient(timeout=15.0) as transport:
            response = await transport.get(url, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Lark Wiki node query failed: HTTP {response.status_code}, invalid response"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(
                f"Lark Wiki node query failed: HTTP {response.status_code} "
                f"{str(payload.get('msg') or '')[:160]}"
            )
        response_code = payload.get("code")
        if response_code is None or int(response_code) != 0:
            raise RuntimeError(
                f"Lark Wiki node query failed: {response_code} "
                f"{str(payload.get('msg') or '')[:160]}"
            )
        node = (payload.get("data") or {}).get("node")
        if not isinstance(node, dict):
            raise RuntimeError("Lark Wiki node query failed: node is missing")
        return node


def _parse_wiki_reference(reference: str) -> tuple[str, str]:
    raw = str(reference or "").strip()
    if not raw:
        raise ValueError("wiki reference is required")
    if raw.startswith(("http://", "https://")):
        parsed = urlsplit(raw)
        segments = [segment for segment in parsed.path.split("/") if segment]
        try:
            token = segments[segments.index("wiki") + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("请粘贴包含 /wiki/<token> 的 Wiki 链接或节点 token") from exc
        query = parse_qs(parsed.query)
        obj_type = str((query.get("obj_type") or [""])[0]).strip()
    else:
        token = raw
        obj_type = ""
    if len(token) > 200 or any(char.isspace() for char in token):
        raise ValueError("Wiki 节点 token 格式无效")
    return token, obj_type[:40]


def _wiki_node_detail(node: object) -> LarkWikiNodeDetail:
    edited_at = _node_value(node, "obj_edit_time", 0) or _node_value(node, "node_create_time", 0) or 0
    try:
        edited_at = int(edited_at)
    except (TypeError, ValueError):
        edited_at = 0
    return LarkWikiNodeDetail(
        title=str(_node_value(node, "title", "") or "")[:200],
        url=str(_node_value(node, "url", "") or "")[:1000],
        obj_type=str(_node_value(node, "obj_type", "") or "")[:40],
        node_type=str(_node_value(node, "node_type", "") or "")[:40],
        has_child=_node_value(node, "has_child", None),
        edited_at=edited_at,
    )


def _node_value(node: object, key: str, default: object = None) -> object:
    if isinstance(node, dict):
        return node.get(key, default)
    return getattr(node, key, default)


def _message_content(raw_content: str) -> str:
    """Extract a short human-readable summary without exposing raw card JSON."""
    try:
        payload = json.loads(raw_content)
    except (TypeError, ValueError):
        return raw_content[:240]
    if isinstance(payload, dict):
        text = payload.get("text") or payload.get("title")
        if text:
            return str(text)[:240]
    return "（卡片或非文本消息）"
