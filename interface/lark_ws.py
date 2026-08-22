from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Protocol

from core.log import get_logger
from interface.lark_access import LarkAccessPolicy
from interface.lark_approval import LarkApprovalManager, PendingApproval
from interface.lark_cards import build_assistant_result_card, build_goal_result_card
from interface.lark_commands import QuickCommandResult
from runtime.contracts import RuntimeHandleResult

log = get_logger("interface.lark_ws")


class AgentProtocol(Protocol):
    async def run_turn(
        self,
        text: str,
        *,
        user_id: str = "default",
        approval_token: str | None = None,
    ) -> str: ...


class CardSenderProtocol(Protocol):
    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None: ...


class QuickCommandProtocol(Protocol):
    async def handle(
        self,
        text: str,
        *,
        user_id: str = "default",
        approval_token: str | None = None,
    ) -> str | QuickCommandResult | None: ...


class RuntimeProtocol(Protocol):
    async def handle_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
        approval_token: str | None = None,
    ) -> RuntimeHandleResult: ...


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
        quick_commands: QuickCommandProtocol | None = None,
        runtime: RuntimeProtocol | None = None,
        access_policy: LarkAccessPolicy | None = None,
        approval_manager: LarkApprovalManager | None = None,
        deduper: LarkMessageDeduper | None = None,
        reconnect_delay_seconds: float = 3.0,
        heartbeat_timeout_seconds: float = 60.0,
    ) -> None:
        self.agent = agent
        self.sender = sender
        self.quick_commands = quick_commands
        self.runtime = runtime
        self.access_policy = access_policy
        self.approval_manager = approval_manager
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
        if self.access_policy is not None and not self.access_policy.is_allowed(
            user_id=user_id,
            chat_id=chat_id,
        ):
            log.warning("lark_access_denied", user_id=user_id, chat_id=chat_id)
            return False

        approved: PendingApproval | None = None
        if self.approval_manager is not None:
            approval_result = self._consume_approval_command(text, user_id=user_id)
            if approval_result == "__CANCELLED__":
                response = "✅ 已取消待确认操作"
                await self.sender.send_card(chat_id, self.build_card(response))
                return True
            if approval_result == "__INVALID__":
                response = "⚠️ 确认码无效或已过期"
                await self.sender.send_card(chat_id, self.build_card(response))
                return True
            if isinstance(approval_result, PendingApproval):
                approved = approval_result
            if approval_result is None and self.approval_manager.requires_confirmation(text):
                pending = self.approval_manager.issue(user_id=user_id, request=text)
                response = (
                    "⚠️ 该请求可能修改系统或数据，暂未执行。\n"
                    f"• 请求：{text.strip()}\n"
                    f"• 确认：`/confirm {pending.token}`\n"
                    "• 取消：`/cancel`\n"
                    f"• 有效期：{int(self.approval_manager.ttl_seconds // 60)} 分钟"
                )
                await self.sender.send_card(chat_id, self.build_card(response))
                log.info("lark_approval_requested", user_id=user_id, chat_id=chat_id)
                return True

        approval_token = approved.token if approved is not None else None
        if approved is not None:
            text = approved.request
        response = None
        response_card: dict[str, Any] | None = None
        if self.quick_commands is not None:
            response = await self.quick_commands.handle(
                text,
                user_id=user_id,
                approval_token=approval_token,
            )
            if response is not None:
                if isinstance(response, QuickCommandResult):
                    response_card = response.card
                    response = response.text
                log.info("lark_quick_command_handled", command=text.strip().lower())
        if response is None:
            if self.runtime is not None:
                runtime_result = await self.runtime.handle_message(
                    user_id=user_id,
                    chat_id=chat_id,
                    text=text,
                    message_id=message_id,
                    approval_token=approval_token,
                )
                if runtime_result.handled:
                    response = runtime_result.summary
                else:
                    response = await self.agent.run_turn(text, user_id=user_id)
            elif approval_token is None:
                response = await self.agent.run_turn(text, user_id=user_id)
            else:
                response = await self.agent.run_turn(
                    text,
                    user_id=user_id,
                    approval_token=approval_token,
                )
        card = response_card or self.build_card(response)
        await self.sender.send_card(chat_id, card)
        log.info(
            "lark_message_processed",
            goal_id=str(event.get("goal_id") or ""),
            duration_ms=int((time.perf_counter() - started) * 1000),
            message_id=message_id,
        )
        return True

    async def send_goal_result(self, goal: dict[str, Any]) -> None:
        """Send a background Goal's terminal result to its owning chat."""
        chat_id = str(goal.get("chat_id") or "")
        if not chat_id:
            return
        status = str(goal.get("status") or "").upper()
        goal_id = str(goal.get("goal_id") or "")[:8]
        result = str(goal.get("result") or "").strip()
        error = str(goal.get("error") or "").strip()
        await self.sender.send_card(
            chat_id,
            build_goal_result_card(
                goal_id=goal_id,
                status=status,
                result=result,
                error=error,
            ),
        )

    def handle_card_action(self, event: dict[str, Any]) -> dict[str, Any]:
        """Handle a Card 2.0 action synchronously on the SDK callback thread."""
        user_id = str(event.get("user_id") or "default")
        chat_id = str(event.get("chat_id") or "")
        if self.access_policy is not None and not self.access_policy.is_allowed(
            user_id=user_id,
            chat_id=chat_id,
        ):
            log.warning("lark_card_access_denied", user_id=user_id, chat_id=chat_id)
            return {"toast": {"type": "error", "content": "无权操作此卡片"}}
        action = event.get("action") or {}
        action_tag = str(action.get("tag") or "")
        if action_tag == "button":
            raw_value = action.get("value")
            page_actions = {"vps_logs_page", "vps_output_page"}
            if isinstance(raw_value, dict) and raw_value.get("action") in page_actions:
                action_name = str(raw_value.get("action"))
                renderer_name = (
                    "render_log_page" if action_name == "vps_logs_page" else "render_output_page"
                )
                renderer = getattr(self.quick_commands, renderer_name, None)
                if not callable(renderer) and action_name == "vps_output_page":
                    renderer = getattr(self.quick_commands, "render_log_page", None)
                if callable(renderer):
                    try:
                        page = int(str(raw_value.get("page") or ""))
                    except ValueError:
                        page = 0
                    result = renderer(
                        str(raw_value.get("token") or ""),
                        page,
                        user_id=user_id,
                    )
                    if isinstance(result, QuickCommandResult) and result.card is not None:
                        log.info(
                            "lark_log_page_selected",
                            user_id=user_id,
                            chat_id=chat_id,
                            page=page,
                        )
                        return {
                            "toast": {"type": "success", "content": f"已切换到第 {page} 页"},
                            "card": {"type": "raw", "data": result.card},
                        }
                    message = (
                        result.text
                        if isinstance(result, QuickCommandResult)
                        else str(result or "日志分页无结果")
                    )
                    return {"toast": {"type": "warning", "content": message[:100]}}
            return {"toast": {"type": "warning", "content": "日志分页操作无效"}}
        if action_tag != "select_static":
            return {"toast": {"type": "warning", "content": "暂不支持此卡片操作"}}
        raw_value = action.get("value")
        if isinstance(raw_value, dict):
            target_id = str(raw_value.get("target_id") or raw_value.get("target") or "")
        else:
            target_id = str(raw_value or action.get("option") or "")
        target_id = target_id.strip()
        selector = getattr(self.quick_commands, "select_target", None)
        if not target_id or selector is None:
            return {"toast": {"type": "error", "content": "目标选择无效"}}
        result = selector(target_id, user_id)
        if isinstance(result, QuickCommandResult):
            text = result.text
        else:
            text = str(result or "目标已更新")
        log.info("lark_target_selected", user_id=user_id, chat_id=chat_id, target_id=target_id)
        return {"toast": {"type": "success", "content": text[:100]}}

    def _consume_approval_command(
        self,
        text: str,
        *,
        user_id: str,
    ) -> PendingApproval | str | None:
        normalized = " ".join(text.strip().split())
        lowered = normalized.lower()
        if lowered in {"/cancel", "cancel", "取消", "/取消"}:
            self.approval_manager.cancel(user_id=user_id)  # type: ignore[union-attr]
            return "__CANCELLED__"
        prefixes = ("/confirm ", "confirm ", "确认 ", "/确认 ")
        for prefix in prefixes:
            if lowered.startswith(prefix):
                token = normalized[len(prefix) :].strip().strip("`")
                request = self.approval_manager.confirm(  # type: ignore[union-attr]
                    user_id=user_id,
                    token=token,
                )
                return request if request is not None else "__INVALID__"
        return None

    def build_card(self, text: str) -> dict[str, Any]:
        return build_assistant_result_card(text)

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
            self._running = True
            self.connected = False
            log.info("lark_websocket_interface_started")
            return None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run_forever(connect_once),
                name="lark-websocket-interface",
            )
        return self._task
