from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Protocol

from core.log import get_logger

log = get_logger("interface.lark_ws")


class AgentProtocol(Protocol):
    async def run_turn(self, text: str, *, user_id: str = "default") -> str: ...


class CardSenderProtocol(Protocol):
    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None: ...


class LarkMessageDeduper:
    def __init__(self, *, ttl_seconds: float = 60.0, max_size: int = 2048) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._seen: OrderedDict[str, float] = OrderedDict()

    def should_process(self, message_id: str) -> bool:
        now = time.time()
        self._evict(now)
        if message_id in self._seen:
            return False
        self._seen[message_id] = now
        self._seen.move_to_end(message_id)
        while len(self._seen) > self.max_size:
            self._seen.popitem(last=False)
        return True

    def _evict(self, now: float) -> None:
        expired = [
            message_id
            for message_id, seen_at in self._seen.items()
            if now - seen_at > self.ttl_seconds
        ]
        for message_id in expired:
            self._seen.pop(message_id, None)


class LarkWebSocketInterface:
    """Message handling core for Lark WebSocket + Card 2.0 replies."""

    def __init__(
        self,
        *,
        agent: AgentProtocol,
        sender: CardSenderProtocol,
        deduper: LarkMessageDeduper | None = None,
        reconnect_delay_seconds: float = 3.0,
        heartbeat_timeout_seconds: float = 60.0,
    ) -> None:
        self.agent = agent
        self.sender = sender
        self.deduper = deduper or LarkMessageDeduper(ttl_seconds=60)
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.last_heartbeat_at = time.time()
        self.connected = False
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._active_handlers: set[asyncio.Task[Any]] = set()

    async def handle_message(self, event: dict[str, Any]) -> bool:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_handlers.add(current_task)
        try:
            return await self._handle_message_once(event)
        finally:
            if current_task is not None:
                self._active_handlers.discard(current_task)

    async def _handle_message_once(self, event: dict[str, Any]) -> bool:
        message_id = str(event.get("message_id") or "")
        if not message_id:
            return False
        if not self.deduper.should_process(message_id):
            log.info("lark_message_deduped", message_id=message_id)
            return False
        self.last_heartbeat_at = time.time()
        user_id = str(event.get("user_id") or "default")
        chat_id = str(event.get("chat_id") or "")
        text = str(event.get("text") or "")
        started = time.perf_counter()
        response = await self.agent.run_turn(text, user_id=user_id)
        card = self.build_card(response)
        await self.sender.send_card(chat_id, card)
        log.info(
            "lark_message_processed",
            goal_id=str(event.get("goal_id") or ""),
            duration_ms=int((time.perf_counter() - started) * 1000),
            message_id=message_id,
        )
        return True

    def build_card(self, text: str) -> dict[str, Any]:
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": text,
                    }
                ]
            },
        }

    def mark_heartbeat(self) -> None:
        self.last_heartbeat_at = time.time()
        self.connected = True

    def heartbeat_ok(self) -> bool:
        return time.time() - self.last_heartbeat_at <= self.heartbeat_timeout_seconds

    async def run_forever(self, connect_once) -> None:
        self._running = True
        while self._running:
            try:
                self.connected = True
                await connect_once(self.handle_message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.error("lark_websocket_disconnected", message=str(exc))
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.connected = False

    async def drain_active(self, timeout_seconds: float = 30.0) -> None:
        if not self._active_handlers:
            return
        await asyncio.wait_for(
            asyncio.gather(*list(self._active_handlers), return_exceptions=True),
            timeout=timeout_seconds,
        )

    def start(self, connect_once=None) -> asyncio.Task[None] | None:
        if connect_once is None:
            self.connected = True
            self.mark_heartbeat()
            log.info("lark_websocket_interface_started")
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run_forever(connect_once),
                name="lark-websocket-interface",
            )
        return self._task
