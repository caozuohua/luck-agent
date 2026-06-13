from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent import AgentApp


class RecordingComponent:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def stop(self) -> None:
        self.calls.append(self.name)


class StalledComponent:
    def __init__(self) -> None:
        self.cancelled = False

    async def stop(self) -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class AgentShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_stops_all_components_and_bounds_stalled_one(
        self,
    ) -> None:
        calls: list[str] = []
        stalled = StalledComponent()
        app = AgentApp.__new__(AgentApp)
        app._runtime_workers = RecordingComponent("workers", calls)
        app._queue = RecordingComponent("queue", calls)
        app._scheduler = stalled
        app._health = RecordingComponent("health", calls)
        ws_runner = RecordingComponent("websocket", calls)

        timed_out = await app._shutdown_components(
            ws_runner=ws_runner,
            timeout=0.01,
        )

        self.assertEqual(
            set(calls),
            {"websocket", "workers", "queue", "health"},
        )
        self.assertEqual(timed_out, ["scheduler"])
        self.assertTrue(stalled.cancelled)

    async def test_shutdown_closes_process_pkb_client(self) -> None:
        calls: list[str] = []
        app = AgentApp.__new__(AgentApp)
        app._runtime_workers = RecordingComponent("workers", calls)
        app._queue = RecordingComponent("queue", calls)
        app._scheduler = RecordingComponent("scheduler", calls)
        app._health = RecordingComponent("health", calls)
        ws_runner = RecordingComponent("websocket", calls)

        async def close_pkb() -> None:
            calls.append("pkb")

        with patch("agent.close_pkb_client", new=close_pkb):
            timed_out = await app._shutdown_components(
                ws_runner=ws_runner,
                timeout=0.1,
            )

        self.assertEqual(timed_out, [])
        self.assertEqual(
            set(calls),
            {"websocket", "workers", "queue", "scheduler", "health", "pkb"},
        )


if __name__ == "__main__":
    unittest.main()
