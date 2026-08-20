from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Awaitable, Callable

from core.log import get_logger


log = get_logger("interface.lark_sdk")

MessageHandler = Callable[[dict[str, Any]], Awaitable[bool]]
StateCallback = Callable[[], None]


def normalize_message_event(data: Any) -> dict[str, Any] | None:
    """Convert lark-oapi's generated event object to the V2 event shape."""

    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    if message is None or sender_id is None:
        return None

    if getattr(message, "message_type", "") != "text":
        log.info(
            "lark_message_ignored",
            reason="unsupported_message_type",
            message_type=getattr(message, "message_type", ""),
        )
        return None

    try:
        content = json.loads(getattr(message, "content", "") or "{}")
    except json.JSONDecodeError:
        log.warning("lark_message_ignored", reason="invalid_message_content")
        return None

    if not isinstance(content, dict):
        return None

    user_id = (
        getattr(sender_id, "open_id", None)
        or getattr(sender_id, "user_id", None)
        or getattr(sender_id, "union_id", None)
        or "default"
    )
    text = str(content.get("text") or "").strip()
    if not text:
        return None

    header = getattr(data, "header", None)
    return {
        "message_id": str(getattr(message, "message_id", "") or ""),
        "chat_id": str(getattr(message, "chat_id", "") or ""),
        "user_id": str(user_id),
        "text": text,
        "event_id": str(getattr(header, "event_id", "") or ""),
    }


class LarkSdkRunner:
    """Run lark-oapi's blocking WebSocket client on its own event loop.

    lark-oapi 1.7.x keeps its WebSocket event loop at module scope. Creating
    the SDK client on the V2 asyncio loop and then moving ``client.start`` to a
    thread causes ``run_until_complete`` to target the already-running V2 loop.
    This runner creates and binds the SDK loop before importing/rebinding the
    SDK WebSocket module, then bridges callbacks back to the V2 loop.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        domain: str,
        application_loop: asyncio.AbstractEventLoop,
        on_message: MessageHandler,
        on_connected: StateCallback | None = None,
        on_disconnected: StateCallback | None = None,
        stop_timeout: float = 10.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain
        self.application_loop = application_loop
        self.on_message = on_message
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.stop_timeout = stop_timeout
        self._thread: threading.Thread | None = None
        self._sdk_loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._stopping = threading.Event()
        self._connected = False
        self._startup_error: BaseException | None = None

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def startup_error(self) -> BaseException | None:
        return self._startup_error

    def start(self) -> None:
        if self.is_alive:
            return
        self._stopping.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run_client,
            name="lark-websocket",
            daemon=True,
        )
        self._thread.start()
        log.info("lark_websocket_started", domain=self.domain)

    async def stop(self) -> None:
        self._stopping.set()
        thread = self._thread
        sdk_loop = self._sdk_loop
        if thread is None or not thread.is_alive():
            return
        if sdk_loop is None or sdk_loop.is_closed():
            await asyncio.to_thread(thread.join, self.stop_timeout)
            return

        shutdown = asyncio.run_coroutine_threadsafe(
            self._shutdown_sdk(),
            sdk_loop,
        )
        await asyncio.wait_for(
            asyncio.wrap_future(shutdown),
            timeout=self.stop_timeout,
        )
        await asyncio.to_thread(thread.join, self.stop_timeout)
        if thread.is_alive():
            raise TimeoutError("Lark WebSocket thread did not stop")
        log.info("lark_websocket_stopped")

    def _run_client(self) -> None:
        sdk_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(sdk_loop)
        self._sdk_loop = sdk_loop

        try:
            # The package root eagerly imports lark_oapi.ws. Rebind the
            # module-level loop before constructing/starting the client.
            import lark_oapi as lark
            import lark_oapi.ws.client as ws_module

            ws_module.loop = sdk_loop
            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._handle_sdk_event)
                .build()
            )
            client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=event_handler,
                domain=self.domain,
                log_level=lark.LogLevel.WARNING,
                auto_reconnect=True,
            )
            client.on_reconnecting = self._mark_disconnected
            client.on_reconnected = self._mark_connected
            self._client = client

            # There is no initial-connect callback in this SDK version. The
            # watcher observes the private connection field and also detects a
            # later disconnect/reconnect without blocking client.start().
            sdk_loop.create_task(
                self._watch_connection(client),
                name="lark-websocket-state",
            )
            client.start()
        except BaseException as error:
            self._startup_error = error
            if not self._stopping.is_set():
                log.error(
                    "lark_websocket_failed",
                    error_type=type(error).__name__,
                    message=str(error),
                )
        finally:
            self._mark_disconnected()
            self._client = None
            self._sdk_loop = None
            if not sdk_loop.is_closed():
                sdk_loop.close()

    async def _watch_connection(self, client: Any) -> None:
        was_connected = False
        while not self._stopping.is_set():
            is_connected = getattr(client, "_conn", None) is not None
            if is_connected and not was_connected:
                self._mark_connected()
            elif was_connected and not is_connected:
                self._mark_disconnected()
            was_connected = is_connected
            await asyncio.sleep(0.5)

    def _handle_sdk_event(self, data: Any) -> None:
        event = normalize_message_event(data)
        if event is None:
            return
        if self.application_loop.is_closed():
            return

        log.info(
            "lark_message_received",
            message_id=event["message_id"],
            chat_id=event["chat_id"],
        )

        future = asyncio.run_coroutine_threadsafe(
            self.on_message(event),
            self.application_loop,
        )
        future.add_done_callback(self._report_message_result)

    @staticmethod
    def _report_message_result(future: Any) -> None:
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception as error:  # pragma: no cover - callback defensive path
            log.error(
                "lark_message_handler_failed",
                error_type=type(error).__name__,
                message=str(error),
            )

    def _mark_connected(self) -> None:
        if self._connected:
            return
        self._connected = True
        if self.on_connected is not None and not self.application_loop.is_closed():
            self.application_loop.call_soon_threadsafe(self.on_connected)
        log.info("lark_websocket_connected")

    def _mark_disconnected(self) -> None:
        if not self._connected:
            return
        self._connected = False
        if self.on_disconnected is not None and not self.application_loop.is_closed():
            self.application_loop.call_soon_threadsafe(self.on_disconnected)
        log.warning("lark_websocket_disconnected")

    async def _shutdown_sdk(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current]
        select_tasks = []
        background_tasks = []
        for task in tasks:
            coro_name = getattr(task.get_coro(), "__name__", "")
            if coro_name == "_select":
                select_tasks.append(task)
            else:
                background_tasks.append(task)

        # Prevent the receive loop from starting another reconnect while its
        # cancellation is being delivered.
        if self._client is not None:
            self._client._auto_reconnect = False

        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        client = self._client
        if client is not None:
            await client._disconnect()

        # lark-oapi's client.start() is blocked in run_until_complete(_select).
        # Do not await this task here: cancelling it makes run_until_complete
        # unwind in the SDK thread, while awaiting it from this coroutine can
        # race with the outer client.start() and leave the loop half-closed.
        for task in select_tasks:
            task.cancel()
