from __future__ import annotations

import asyncio
import json
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

from core.log import get_logger


log = get_logger("interface.lark_api")


class LarkApiSender:
    """Send Card 2.0 messages through the Lark REST API."""

    def __init__(self, client: lark.Client) -> None:
        self.client = client

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None:
        if not chat_id:
            raise ValueError("chat_id is required to send a Lark card")

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )

        # The generated SDK's sync request path uses requests. Keep it off the
        # application event loop so a slow Lark API call cannot block health,
        # shutdown, or other conversations.
        response = await asyncio.to_thread(
            self.client.im.v1.message.create,
            request,
        )
        if response.code != 0:
            log.error(
                "lark_send_card_failed",
                code=response.code,
                lark_error_message=response.msg,
                http_status=(response.raw.status_code if response.raw else None),
            )
            raise RuntimeError(
                f"Lark send message failed: {response.code} {response.msg}"
            )
        log.info("lark_card_sent", chat_id=chat_id)
