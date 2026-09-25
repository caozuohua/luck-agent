from __future__ import annotations

import asyncio
import json
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import AsyncMock

from interface.web import WebInterface


class _FakeAgent:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_text: str | None = None

    async def run_turn(self, text: str, *, user_id: str = "default") -> str:
        self.last_text = text
        return self.reply


class WebInterfaceTests(unittest.TestCase):
    def _start(self, agent: _FakeAgent) -> WebInterface:
        iface = WebInterface(agent=agent, host="127.0.0.1", port=0)
        # bind to an ephemeral port
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        iface.port = port
        iface.start()
        return iface

    def test_chat_returns_reply(self) -> None:
        agent = _FakeAgent("hello-from-agent")
        iface = self._start(agent)
        try:
            conn = HTTPConnection("127.0.0.1", iface.port, timeout=5)
            body = json.dumps({"text": "hi there"}).encode()
            conn.request("POST", "/chat", body=body,
                        headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
        finally:
            asyncio.run(iface.stop())
        self.assertEqual(resp.status, 200)
        self.assertEqual(data["reply"], "hello-from-agent")
        self.assertEqual(agent.last_text, "hi there")

    def test_root_serves_html(self) -> None:
        agent = _FakeAgent("x")
        iface = self._start(agent)
        try:
            conn = HTTPConnection("127.0.0.1", iface.port, timeout=5)
            conn.request("GET", "/")
            resp = conn.getresponse()
            html = resp.read().decode()
            conn.close()
        finally:
            asyncio.run(iface.stop())
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type", ""))
        self.assertIn("Luck Agent V2", html)

    def test_empty_text_rejected(self) -> None:
        agent = _FakeAgent("should-not-be-called")
        iface = self._start(agent)
        try:
            conn = HTTPConnection("127.0.0.1", iface.port, timeout=5)
            conn.request("POST", "/chat",
                        body=json.dumps({"text": "  "}).encode(),
                        headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
        finally:
            asyncio.run(iface.stop())
        self.assertEqual(resp.status, 400)
        self.assertIsNone(agent.last_text)

    def test_special_chars_dont_crash(self) -> None:
        # A "/" or "(" in the query used to raise fts5: syntax error
        # near "/" and crash the whole turn. The agent should still
        # return a reply (the pattern search must quote safely).
        agent = _FakeAgent("handled /path and (parens) fine")
        iface = self._start(agent)
        try:
            conn = HTTPConnection("127.0.0.1", iface.port, timeout=5)
            conn.request(
                "POST", "/chat",
                body=json.dumps({"text": "查 /etc/config 和 (foo)"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
        finally:
            asyncio.run(iface.stop())
        self.assertEqual(resp.status, 200)
        self.assertEqual(data["reply"], "handled /path and (parens) fine")
        self.assertEqual(agent.last_text, "查 /etc/config 和 (foo)")


    def test_shutdown_endpoint_returns_ok(self) -> None:
        # Replace only this server's shutdown target. Restoring a process-wide
        # os._exit patch before the background thread finishes can kill pytest
        # with exit code 0 and silently hide the rest of the suite.
        import interface.web as web_mod

        agent = _FakeAgent("x")
        iface = self._start(agent)
        shutdown_called = threading.Event()

        def shutdown_server(handler) -> None:
            handler.server.shutdown()
            shutdown_called.set()

        iface._server.RequestHandlerClass._shutdown_server = shutdown_server
        try:
            conn = HTTPConnection("127.0.0.1", iface.port, timeout=5)
            conn.request("POST", web_mod.SHUTDOWN_ENDPOINT)
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertEqual(data.get("status"), "shutting down")
            self.assertTrue(shutdown_called.wait(timeout=5))
        finally:
            # The _shutdown_server thread may have called server.shutdown();
            # ensure we don't leak a serving loop.
            try:
                asyncio.run(iface.stop())
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
