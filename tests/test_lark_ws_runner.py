from __future__ import annotations

import asyncio
import threading
import unittest

from core.lark_ws_runner import LarkWebSocketRunner


class FakeLarkClient:
    def __init__(self, sdk_loop: asyncio.AbstractEventLoop) -> None:
        self.sdk_loop = sdk_loop
        self.started = threading.Event()
        self.disconnected = False

    def start(self) -> None:
        self.started.set()
        self.sdk_loop.run_until_complete(self._serve_forever())

    async def _serve_forever(self) -> None:
        await asyncio.Future()

    async def _disconnect(self) -> None:
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

        runner.start()
        started = await asyncio.to_thread(client.started.wait, 1.0)
        self.assertTrue(started)

        await runner.stop()
        await runner.stop()

        self.assertTrue(client.disconnected)
        self.assertFalse(runner.is_alive)
        sdk_loop.close()


if __name__ == "__main__":
    unittest.main()
