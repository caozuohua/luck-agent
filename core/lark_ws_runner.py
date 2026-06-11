from __future__ import annotations

import asyncio
import threading
from typing import Protocol

from core.log import get_logger


log = get_logger()


class LarkClientProtocol(Protocol):
    def start(self) -> None: ...

    async def _disconnect(self) -> None: ...


class LarkWebSocketRunner:
    """Own the blocking lark-oapi WebSocket thread and stop it predictably."""

    def __init__(
        self,
        *,
        client: LarkClientProtocol,
        sdk_loop: asyncio.AbstractEventLoop,
        stop_timeout: float = 10.0,
    ) -> None:
        self.client = client
        self.sdk_loop = sdk_loop
        self.stop_timeout = stop_timeout
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._stop_task: asyncio.Task[None] | None = None

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.is_alive:
            return
        self._stopping.clear()
        self._stop_task = None
        self._thread = threading.Thread(
            target=self._run_client,
            name="lark-websocket",
            daemon=True,
        )
        self._thread.start()

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_once(),
                name="lark-websocket-stop",
            )
        await asyncio.shield(self._stop_task)

    def _run_client(self) -> None:
        try:
            self.client.start()
        except BaseException as error:
            if not self._stopping.is_set():
                log.error(
                    "lark_websocket_failed",
                    error_type=type(error).__name__,
                )

    async def _stop_once(self) -> None:
        self._stopping.set()
        if not self.is_alive:
            return
        if self.sdk_loop.is_closed():
            raise RuntimeError("Lark SDK event loop is closed")

        shutdown = asyncio.run_coroutine_threadsafe(
            self._shutdown_sdk(),
            self.sdk_loop,
        )
        await asyncio.wait_for(
            asyncio.wrap_future(shutdown),
            timeout=self.stop_timeout,
        )
        assert self._thread is not None
        await asyncio.to_thread(self._thread.join, self.stop_timeout)
        if self._thread.is_alive():
            raise TimeoutError("Lark WebSocket thread did not stop")
        log.info("lark_websocket_stopped")

    async def _shutdown_sdk(self) -> None:
        current = asyncio.current_task()
        background_tasks: list[asyncio.Task] = []
        select_tasks: list[asyncio.Task] = []
        for task in asyncio.all_tasks():
            if task is current:
                continue
            coro = task.get_coro()
            coro_name = getattr(coro, "__name__", "")
            if coro_name.startswith("_select"):
                select_tasks.append(task)
            else:
                background_tasks.append(task)

        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(
                *background_tasks,
                return_exceptions=True,
            )

        await self.client._disconnect()
        loop = asyncio.get_running_loop()
        for task in select_tasks:
            loop.call_soon(task.cancel)
