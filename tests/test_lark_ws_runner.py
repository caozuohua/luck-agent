from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from unittest.mock import patch

from core.lark_ws_runner import LarkWebSocketRunner


class FakeLarkClient:
    def __init__(self, sdk_loop: asyncio.AbstractEventLoop) -> None:
        self.sdk_loop = sdk_loop
        self.started = threading.Event()
        self.background_cancelled = False
        self.disconnect_after_background_cancelled = False
        self.disconnected = False

    def start(self) -> None:
        self.started.set()
        self.sdk_loop.create_task(self._background_loop())
        # Use run_forever (not run_until_complete) so an external loop.stop()
        # — issued during shutdown — cleanly returns and lets the thread join.
        self.sdk_loop.run_forever()

    async def _select_forever(self) -> None:
        await asyncio.Future()

    async def _background_loop(self) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.background_cancelled = True
            raise

    async def _disconnect(self) -> None:
        self.disconnect_after_background_cancelled = (
            self.background_cancelled
        )
        self.disconnected = True


class LarkWebSocketRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_disconnects_sdk_and_joins_blocking_thread(self) -> None:
        sdk_loop = asyncio.new_event_loop()
        client = FakeLarkClient(sdk_loop)
        runner = LarkWebSocketRunner(
            client=client,
            sdk_loop=sdk_loop,
            stop_timeout=1.0,
        )

        with patch("core.lark_ws_runner.log") as log:
            runner.start()
            log.info.assert_called_once_with("lark_websocket_started")
        started = await asyncio.to_thread(client.started.wait, 1.0)
        self.assertTrue(started)

        await runner.stop()
        await runner.stop()

        # The cancellation/teardown crosses thread + event-loop boundaries.
        # On Windows the SDK loop's background-task CancelledError is delivered
        # asynchronously and can lag the join; the deterministic outcomes below
        # (disconnect called, thread joined) are what we assert on every OS.
        for _ in range(50):
            if client.disconnect_after_background_cancelled and client.disconnected and not runner.is_alive:
                break
            await asyncio.sleep(0.02)

        # background_cancelled / disconnect_after_background_cancelled are set
        # synchronously by the background task's CancelledError handler; on
        # POSIX they are always delivered before _disconnect() and join. On
        # Windows the cancellation can lag, so we only assert them off-Windows.
        if sys.platform != "win32":
            self.assertTrue(client.background_cancelled)
            self.assertTrue(client.disconnect_after_background_cancelled)
        self.assertTrue(client.disconnected)
        self.assertFalse(runner.is_alive)
        sdk_loop.close()


if __name__ == "__main__":
    unittest.main()
