"""Local web interface for the V2 runtime — a lightweight test harness.

Replaces the Lark WebSocket interface when no Lark credentials are
configured, so the runtime can be exercised from a browser / curl without
a Lark app. Pure stdlib (http.server) — no extra dependency.

The class mirrors the surface of `LarkWebSocketInterface`
(`start` / `stop` / `drain_active`) so `main.py` can swap it in.

Routes:
  GET  /       -> tiny chat page (HTML form)
  POST /chat   -> {"text": "..."}  (JSON or form) -> {"reply": "..."}
"""
from __future__ import annotations

import asyncio
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from core.log import get_logger

log = get_logger()


class AgentProtocol(Protocol):
    async def run_turn(self, text: str, *, user_id: str = "default") -> str: ...


class WebInterface:
    """Serve the agent over a local HTTP page for manual testing."""

    def __init__(
        self,
        *,
        agent: AgentProtocol,
        host: str = "127.0.0.1",
        port: int = 8000,
        user_id: str = "default",
    ) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self.user_id = user_id
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── interface surface (matches LarkWebSocketInterface) ──────────────
    def start(self) -> None:
        if self._server is not None:
            return
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="web-interface",
            daemon=True,
        )
        self._thread.start()
        log.info("web_interface_started", host=self.host, port=self.port)

    async def drain_active(self, timeout_seconds: float = 30.0) -> None:
        # Web requests are handled synchronously per-call; nothing to drain.
        return None

    async def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        log.info("web_interface_stopped")

    # ── internals ───────────────────────────────────────────────────────
    def _make_handler(self):
        agent = self.agent
        user_id = self.user_id

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence default stderr logging
                pass

            def do_GET(self):  # noqa: N802
                if self.path.split("?")[0] == "/":
                    self._send_html(_CHAT_PAGE)
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                if self.path.split("?")[0] != "/chat":
                    self._send_json(404, {"error": "not found"})
                    return
                body = self._read_body()
                if not body:
                    self._send_json(400, {"error": "empty body"})
                    return
                # Accept either JSON {"text": ...} or form-encoded `text`.
                try:
                    payload = json.loads(body)
                    text = str(payload.get("text", "")).strip()
                except (json.JSONDecodeError, AttributeError):
                    text = body.decode("utf-8", "replace").strip()
                if not text:
                    self._send_json(400, {"error": "missing 'text'"})
                    return
                reply = asyncio.run(agent.run_turn(text, user_id=user_id))
                self._send_json(200, {"reply": reply})

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length", "0") or "0")
                return self.rfile.read(length) if length else b""

            def _send_json(self, code: int, obj: Any) -> None:
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_html(self, markup: str) -> None:
                data = markup.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return _Handler

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


_CHAT_PAGE = """<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Luck Agent V2 — 本地测试</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.2rem; }
    #log { border: 1px solid #ccc; border-radius: 8px; padding: 1rem; min-height: 200px; white-space: pre-wrap; }
    .you { color: #0a6; font-weight: 600; }
    .bot { color: #06c; }
    textarea { width: 100%; height: 4rem; }
    button { margin-top: .5rem; padding: .4rem 1rem; }
  </style>
</head>
<body>
  <h1>Luck Agent V2 — 本地测试界面</h1>
  <div id="log"></div>
  <textarea id="text" placeholder="输入消息，回车发送"></textarea>
  <button id="send">发送</button>
  <script>
    const log = document.getElementById('log');
    const text = document.getElementById('text');
    function add(cls, msg) {
      const d = document.createElement('div');
      d.className = cls;
      d.textContent = (cls === 'you' ? '你: ' : 'Agent: ') + msg;
      log.appendChild(d);
      log.scrollTop = log.scrollHeight;
    }
    async function send() {
      const t = text.value.trim();
      if (!t) return;
      add('you', t);
      text.value = '';
      const r = await fetch('/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: t})
      });
      const j = await r.json();
      add('bot', j.reply || JSON.stringify(j));
    }
    document.getElementById('send').onclick = send;
    text.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  </script>
</body>
</html>
"""
