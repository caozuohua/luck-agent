from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetChatRequest, ListMessageRequest


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
