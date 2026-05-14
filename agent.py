"""
agent.py — 主入口
Lark WebSocket 长连接 + 全组件装配 + 优雅退出
"""
from __future__ import annotations

import asyncio
import json
import signal
import time
from typing import Any

import lark_oapi as lark
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()


# ─── Lark 发消息工具函数 ──────────────────────────────────────────────────────
class LarkSender:
    """封装 Lark 消息发送（文本 + 卡片），处理 reply 和主动发送。"""

    def __init__(self, client: lark.Client) -> None:
        self._client = client

    async def send(
        self,
        chat_id: str,
        text: str | None = None,
        card: dict | None = None,
        reply_to: str | None = None,   # message_id，非 None 则用 reply 接口
    ) -> None:
        loop = asyncio.get_running_loop()

        if card:
            msg_type = "interactive"
            content  = json.dumps(card, ensure_ascii=False)
        elif text:
            msg_type = "text"
            content  = json.dumps({"text": text}, ensure_ascii=False)
        else:
            return

        def _do() -> None:
            try:
                if reply_to:
                    from lark_oapi.api.im.v1 import (
                        ReplyMessageRequest, ReplyMessageRequestBody,
                    )
                    req = (
                        ReplyMessageRequest.builder()
                        .message_id(reply_to)
                        .request_body(
                            ReplyMessageRequestBody.builder()
                            .content(content)
                            .msg_type(msg_type)
                            .build()
                        )
                        .build()
                    )
                    resp = self._client.im.v1.message.reply(req)
                else:
                    from lark_oapi.api.im.v1 import (
                        CreateMessageRequest, CreateMessageRequestBody,
                    )
                    req = (
                        CreateMessageRequest.builder()
                        .receive_id_type("chat_id")
                        .request_body(
                            CreateMessageRequestBody.builder()
                            .receive_id(chat_id)
                            .content(content)
                            .msg_type(msg_type)
                            .build()
                        )
                        .build()
                    )
                    resp = self._client.im.v1.message.create(req)

                if not resp.success():
                    log.error("lark_send_failed", code=resp.code, msg=resp.msg)
            except Exception as e:
                log.error("lark_send_error", error=str(e))

        await loop.run_in_executor(None, _do)


# ─── 主 Agent 应用 ────────────────────────────────────────────────────────────
class AgentApp:
    """组装所有组件，驱动 WebSocket 事件循环。"""

    def __init__(self) -> None:
        from config import cfg
        self.cfg = cfg

        # 延迟初始化（等 secrets 加载后）
        self._lark_client: lark.Client | None      = None
        self._sender:      LarkSender | None        = None
        self._memory                                = None
        self._router                                = None
        self._queue                                 = None
        self._github                                = None
        self._shell                                 = None
        self._file_mgr                              = None
        self._bridge                                = None
        self._cmd_handler                           = None
        self._msg_handler                           = None
        self._file_handler                          = None

    def _init_components(self) -> None:
        """所有组件在 secrets 加载完成后初始化。"""
        from core.memory       import Memory
        from core.model_router import ModelRouter
        from core.task_queue   import TaskQueue
        from tools.github_tools import GitHubClient
        from tools.shell_tools  import ShellExecutor, FileManager
        from tools.file_bridge  import FileBridge
        from cards.builder      import CardBuilder
        from handlers.command   import CommandHandler
        from handlers.message   import AgentMessageHandler
        from handlers.file_handler import FileMessageHandler

        cfg = self.cfg

        # Lark
        self._lark_client = (
            lark.Client.builder()
            .app_id(cfg.LARK_APP_ID)
            .app_secret(cfg.LARK_APP_SECRET)
            .build()
        )
        self._sender = LarkSender(self._lark_client)

        # 快捷发送函数（绑定到 reply_fn 签名）
        async def reply_fn(chat_id: str, text: str | None = None, card: dict | None = None):
            await self._sender.send(chat_id, text=text, card=card)

        # 核心组件
        self._memory   = Memory(cfg.DB_PATH)
        self._router   = ModelRouter(cfg.GCP_PROJECT, cfg.GCP_LOCATION)
        self._queue    = TaskQueue(workers=cfg.TASK_WORKERS, memory=self._memory)
        self._github   = GitHubClient(cfg.GITHUB_TOKEN, cfg.GITHUB_DEFAULT_OWNER)
        self._shell    = ShellExecutor(cfg.SHELL_WORK_DIR, cfg.SHELL_TIMEOUT, cfg.SHELL_MAX_OUTPUT)
        self._file_mgr = FileManager(cfg.FILE_UPLOAD_DIR)
        self._bridge   = FileBridge(cfg.LARK_APP_ID, cfg.LARK_APP_SECRET,
                                    cfg.FILE_UPLOAD_DIR, cfg.FILE_MAX_SIZE_MB, domain=cfg.LARK_DOMAIN)

        # 任务完成通知回调
        async def task_notify(task):
            from cards.builder import CardBuilder as CB
            # 从 memory 找到 user 的 chat_id（简化：存到 profile 里）
            chat_id = self._memory.get_profile(task.user_id, "default_chat_id", "")
            if chat_id:
                await self._sender.send(
                    chat_id,
                    card=CB.task_status(task.task_id, task.task_type,
                                        task.status.value, task.result, task.error),
                )

        # Handlers
        self._cmd_handler = CommandHandler(
            shell=self._shell,
            files=self._file_mgr,
            bridge=self._bridge,
            github=self._github,
            memory=self._memory,
            queue=self._queue,
            card=CardBuilder,
            lark_reply_fn=reply_fn,
            hugo_repo=cfg.HUGO_REPO,
        )
        self._msg_handler = AgentMessageHandler(
            config=cfg,
            memory=self._memory,
            router=self._router,
            queue=self._queue,
            github=self._github,
            shell=self._shell,
            file_mgr=self._file_mgr,
            card=CardBuilder,
            lark_reply_fn=reply_fn,
        )
        self._file_handler = FileMessageHandler(
            bridge=self._bridge,
            card=CardBuilder,
            lark_reply_fn=reply_fn,
        )

        log.info("components_initialized")

    # ── 事件处理 ──────────────────────────────────────────────────────
    async def _on_message(self, event_data: dict) -> None:
        """统一消息入口，派发到 command / file / ai handler。"""
        try:
            event   = event_data.get("event", {})
            msg     = event.get("message", {})
            sender  = event.get("sender", {})

            chat_id    = msg.get("chat_id", "")
            message_id = msg.get("message_id", "")
            msg_type   = msg.get("message_type", "text")
            chat_type  = msg.get("chat_type", "p2p")
            user_id    = sender.get("sender_id", {}).get("open_id", "")

            if not user_id or not chat_id:
                return

            # 记录用户默认 chat_id（用于任务完成通知）
            self._memory.set_profile(user_id, "default_chat_id", chat_id)

            # 文件/图片类型
            if msg_type in ("image", "file", "audio"):
                asyncio.create_task(
                    self._file_handler.handle(user_id, chat_id, message_id, msg)
                )
                return

            # 文本消息
            content_raw = msg.get("content", "{}")
            try:
                text = json.loads(content_raw).get("text", "").strip()
            except Exception:
                text = ""

            if not text:
                return

            # 群聊去掉 @mention
            if chat_type == "group":
                mentions = msg.get("mentions", [])
                bot_mentioned = any(
                    m.get("key", "").startswith("@_user_") for m in mentions
                )
                if not bot_mentioned:
                    return
                # 移除 @xxx 前缀
                import re
                text = re.sub(r"@\S+\s*", "", text).strip()
                if not text:
                    return

            log.info("message_in", user_id=user_id[:8], type=msg_type,
                     chat_type=chat_type, length=len(text))

            # 指令优先（大模型无关）
            handled = await self._cmd_handler.handle(user_id, chat_id, message_id, text)
            if handled:
                return

            # 转给 AI
            asyncio.create_task(
                self._msg_handler.handle(user_id, chat_id, message_id, text)
            )

        except Exception as e:
            log.error("on_message_error", error=str(e))

    # ── 启动 ──────────────────────────────────────────────────────────
    async def run(self) -> None:
        log.info("agent_starting")

        # 加载 secrets
        #self.cfg.load_secrets()
        self.cfg.load()

        # 初始化组件
        self._init_components()

        # 启动任务队列
        await self._queue.start()

        # 构建 WS 事件分发
        def _make_lark_handler():
            def _sync_handler(data):
                # lark-oapi 回调是同步的，转成 asyncio task
                try:
                    loop = asyncio.get_event_loop()
                    if isinstance(data, dict):
                        event_data = data
                    else:
                        # SDK 对象 → dict
                        event_data = json.loads(lark.JSON.marshal(data))
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(self._on_message(event_data))
                    )
                except Exception as e:
                    log.error("lark_handler_error", error=str(e))
            return _sync_handler

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_make_lark_handler())
            .build()
        )

        ws_client = lark.ws.Client(
            self.cfg.LARK_APP_ID,
            self.cfg.LARK_APP_SECRET,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            domain=self.cfg.LARK_DOMAIN,
        )

        # 优雅退出
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        # 在线程池里启动 WS（阻塞调用）
        ws_task = loop.run_in_executor(None, ws_client.start)

        log.info("agent_running", mode="websocket",
                 hugo_repo=self.cfg.HUGO_REPO,
                 model_pro=self.cfg.MODEL_PRO)

        await stop_event.wait()

        log.info("shutting_down")
        await self._queue.stop()

        # 等待进行中任务
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        log.info("agent_stopped")


# ─── 入口 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(AgentApp().run())
