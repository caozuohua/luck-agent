"""
handlers/file_handler.py — Lark 文件消息处理器
接收 Lark 发来的文件/图片，保存到 VPS，发送确认卡片。
大模型不可用时仍然正常工作。
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from core.log import get_logger

if TYPE_CHECKING:
    from tools.file_bridge import FileBridge
    from cards.builder import CardBuilder

log = get_logger()


class FileMessageHandler:
    """处理 Lark 文件/图片类型消息。"""

    def __init__(
        self,
        bridge: "FileBridge",
        card: type["CardBuilder"],
        lark_reply_fn,
        ai_handler=None,         # 可选：接收后用 AI 分析文件内容
    ) -> None:
        self.bridge  = bridge
        self.card    = card
        self.reply   = lark_reply_fn
        self.ai      = ai_handler

    async def handle(
        self,
        user_id: str,
        chat_id: str,
        message_id: str,
        msg: dict,
    ) -> None:
        """
        处理文件/图片消息。
        msg 结构：Lark event 中的 message 字段。
        """
        msg_type = msg.get("message_type", "")
        content_raw = msg.get("content", "{}")

        try:
            content = json.loads(content_raw)
        except Exception:
            content = {}

        try:
            if msg_type == "image":
                await self._handle_image(user_id, chat_id, content)
            elif msg_type == "file":
                await self._handle_file(user_id, chat_id, content)
            elif msg_type == "audio":
                await self._handle_audio(user_id, chat_id, content)
            else:
                log.debug("unsupported_file_type", msg_type=msg_type)
        except Exception as e:
            log.error("file_handler_error", error=str(e), user_id=user_id[:8])
            await self.reply(
                chat_id,
                card=self.card.error("文件接收失败", str(e)),
            )

    async def _handle_image(self, user_id: str, chat_id: str, content: dict) -> None:
        image_key = content.get("image_key", "")
        if not image_key:
            return

        result = await self.bridge.download_from_lark(
            file_key=image_key,
            file_name=f"image_{image_key[:8]}.jpg",
            message_type="image",
        )
        await self.reply(
            chat_id,
            card=self.card.file_received(
                file_name=result["file_name"],
                size=result["size"],
                local_path=result["local_path"],
            ),
        )
        log.info("image_saved", user_id=user_id[:8], path=result["local_path"])

    async def _handle_file(self, user_id: str, chat_id: str, content: dict) -> None:
        file_key  = content.get("file_key", "")
        file_name = content.get("file_name", f"file_{file_key[:8]}")
        if not file_key:
            return

        result = await self.bridge.download_from_lark(
            file_key=file_key,
            file_name=file_name,
            message_type="file",
        )
        await self.reply(
            chat_id,
            card=self.card.file_received(
                file_name=result["file_name"],
                size=result["size"],
                local_path=result["local_path"],
            ),
        )
        log.info("file_saved", user_id=user_id[:8], name=file_name,
                 path=result["local_path"])

    async def _handle_audio(self, user_id: str, chat_id: str, content: dict) -> None:
        file_key = content.get("file_key", "")
        if not file_key:
            return
        result = await self.bridge.download_from_lark(
            file_key=file_key,
            file_name=f"audio_{file_key[:8]}.opus",
            message_type="file",
        )
        await self.reply(
            chat_id,
            card=self.card.file_received(
                file_name=result["file_name"],
                size=result["size"],
                local_path=result["local_path"],
            ),
        )
