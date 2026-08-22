from __future__ import annotations

import asyncio
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetChatRequest


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
