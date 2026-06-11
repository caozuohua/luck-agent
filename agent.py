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
from core.auth import is_authorized_user
from core.lark_ws_runner import LarkWebSocketRunner
from core.log import get_logger
from handlers.message import forward_to_pkb_result, parse_note_message

log = get_logger()


# ─── Lark 发消息工具函数 ──────────────────────────────────────────────────────
class LarkSender:
    """封装 Lark 消息发送（文本 + 卡片），处理 reply 和主动发送。"""

    # Lark 实际限制：文本消息 4096 字符，卡片 markdown 元素约 4000 字符
    TEXT_CHUNK_SIZE = 3800   # 留 buffer，按自然段切割
    CARD_CHUNK_SIZE = 3500

    def __init__(self, client: lark.Client) -> None:
        self._client = client

    async def send(
        self,
        chat_id: str,
        text: str | None = None,
        card: dict | None = None,
        reply_to: str | None = None,
    ) -> None:
        if card:
            # 卡片：检查 markdown 元素是否超长，超长则拆分
            await self._send_card_chunked(chat_id, card, reply_to)
        elif text:
            # 文本：按 chunk size 分片发送
            chunks = self._split_text(text, self.TEXT_CHUNK_SIZE)
            for i, chunk in enumerate(chunks):
                # 多片时在末尾标注页码
                if len(chunks) > 1:
                    chunk = f"{chunk}\n\n`{i+1}/{len(chunks)}`"
                await self._send_raw(chat_id, "text",
                                     json.dumps({"text": chunk}, ensure_ascii=False),
                                     reply_to if i == 0 else None)
                if len(chunks) > 1:
                    await asyncio.sleep(0.3)   # 避免发送过快被限速

    async def _send_card_chunked(self, chat_id: str, card: dict,
                                  reply_to: str | None) -> None:
        """检查卡片 body 中的 markdown 元素，超长时拆成多张卡片发送。"""
        elements = card.get("body", {}).get("elements", [])
        chunks   = self._split_card_elements(elements, self.CARD_CHUNK_SIZE)

        for i, elem_chunk in enumerate(chunks):
            chunk_card = dict(card)   # shallow copy
            chunk_card["body"] = {"elements": elem_chunk}
            # 多张时在 header title 加页码
            if len(chunks) > 1:
                header = dict(card.get("header", {}))
                title_content = header.get("title", {}).get("content", "")
                header["title"] = {"tag": "plain_text",
                                   "content": f"{title_content} ({i+1}/{len(chunks)})"}
                chunk_card["header"] = header

            await self._send_raw(chat_id, "interactive",
                                 json.dumps(chunk_card, ensure_ascii=False),
                                 reply_to if i == 0 else None)
            if len(chunks) > 1:
                await asyncio.sleep(0.4)

    def _split_text(self, text: str, chunk_size: int) -> list[str]:
        """按段落边界切割长文本，尽量不在段落中间断开。"""
        if len(text) <= chunk_size:
            return [text]

        chunks, current = [], []
        current_len = 0

        for para in text.split("\n"):
            para_len = len(para) + 1  # +1 for newline
            if current_len + para_len > chunk_size and current:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            # 单段超长时强制切
            if para_len > chunk_size:
                for j in range(0, len(para), chunk_size):
                    chunks.append(para[j:j + chunk_size])
            else:
                current.append(para)
                current_len += para_len

        if current:
            chunks.append("\n".join(current))
        return chunks

    def _split_card_elements(self, elements: list[dict],
                              chunk_size: int) -> list[list[dict]]:
        """
        把卡片 elements 按 markdown content 总长度切割。
        非 markdown 元素（hr、div 等）按原样保留在当前片。
        """
        chunks, current, current_len = [], [], 0

        for elem in elements:
            content = elem.get("content", "") if elem.get("tag") == "markdown" else ""
            elem_len = len(content)

            # markdown 元素本身超长 → 拆分这个元素
            if elem.get("tag") == "markdown" and elem_len > chunk_size:
                for text_chunk in self._split_text(content, chunk_size):
                    if current:
                        chunks.append(current)
                    chunks.append([{"tag": "markdown", "content": text_chunk}])
                    current, current_len = [], 0
                continue

            if current_len + elem_len > chunk_size and current:
                chunks.append(current)
                current, current_len = [], 0

            current.append(elem)
            current_len += elem_len

        if current:
            chunks.append(current)
        return chunks if chunks else [elements]

    async def _send_raw(self, chat_id: str, msg_type: str,
                        content: str, reply_to: str | None) -> None:
        """底层发送，不做分片。"""
        loop = asyncio.get_running_loop()

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

    SHUTDOWN_TIMEOUT = 15.0

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
        self._runtime_manager                       = None
        self._runtime_workers                       = None

    def _init_components(self) -> None:
        """所有组件在 secrets 加载完成后初始化。"""
        from core.memory       import Memory
        from core.model_router import ModelRouter
        from core.task_queue   import TaskQueue
        from core.scheduler    import Scheduler, ScheduleStore
        from core.health       import HealthMonitor, DBLogHandler
        from tools.github_tools import GitHubClient
        from tools.shell_tools  import ShellExecutor, FileManager
        from tools.file_bridge  import FileBridge
        from cards.builder      import CardBuilder
        from handlers.command   import CommandHandler
        from handlers.message   import AgentMessageHandler
        from handlers.file_handler import FileMessageHandler
        from controllers.content_generator import ModelContentGenerator
        from core.execution_engine import ExecutionEngine
        from core.goal import GoalManager
        from core.supervisor import Supervisor
        from runtime.events import RuntimeEventRecorder
        from runtime.notifications import AcceptanceGatedNotifier, RuntimeGoalNotifier
        from runtime.runtime_manager import RuntimeManager
        from runtime.task_queue import RuntimeTaskQueue
        from runtime.worker import WorkerManager
        from skills.blog import BlogSkill
        from skills.legacy_react import LegacyReactSkill
        from skills.registry import SkillRegistry
        from skills.router import SkillRouter

        cfg = self.cfg

        # Lark
        self._lark_client = (
            lark.Client.builder()
            .app_id(cfg.LARK_APP_ID)
            .app_secret(cfg.LARK_APP_SECRET)
            .domain(cfg.LARK_DOMAIN)
            .build()
        )
        self._sender = LarkSender(self._lark_client)

        # 快捷发送函数（绑定到 reply_fn 签名）
        async def reply_fn(chat_id: str, text: str | None = None, card: dict | None = None):
            await self._sender.send(chat_id, text=text, card=card)

        # ── 日志回溯处理器（error/warning 写入 SQLite）──────────────
        self._db_log = DBLogHandler(cfg.DB_PATH)

        # DBLogHandler 作为标准 logging handler 注入（不依赖 structlog）
        import logging as _logging
        _logging.getLogger().addHandler(self._db_log)

        # 核心组件
        self._memory   = Memory(cfg.DB_PATH)
        self._router   = ModelRouter(cfg.GCP_PROJECT, cfg.GCP_LOCATION)
        self._queue    = TaskQueue(workers=cfg.TASK_WORKERS, memory=self._memory)
        self._github   = GitHubClient(cfg.GITHUB_TOKEN, cfg.GITHUB_DEFAULT_OWNER)
        self._shell    = ShellExecutor(cfg.SHELL_WORK_DIR, cfg.SHELL_TIMEOUT, cfg.SHELL_MAX_OUTPUT)
        self._file_mgr = FileManager(cfg.FILE_UPLOAD_DIR)
        self._bridge   = FileBridge(cfg.LARK_APP_ID, cfg.LARK_APP_SECRET,
                                    cfg.FILE_UPLOAD_DIR, cfg.FILE_MAX_SIZE_MB,
                                    domain=cfg.LARK_DOMAIN)

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

        # Goal Runtime: selected intents are persisted and queued for background execution.
        goal_manager = GoalManager(self._memory)
        runtime_queue = RuntimeTaskQueue(max_active=1)
        generator = ModelContentGenerator(router=self._router, model_name=cfg.MODEL_PRO)
        registry = SkillRegistry([BlogSkill(generator=generator), LegacyReactSkill()])
        skill_router = SkillRouter(registry)
        event_recorder = RuntimeEventRecorder(self._memory)
        execution_engine = ExecutionEngine(
            goal_manager=goal_manager,
            supervisor=Supervisor(memory=self._memory),
            skill_registry=registry,
            event_recorder=event_recorder,
        )
        self._runtime_manager = RuntimeManager(
            goal_manager=goal_manager,
            execution_engine=execution_engine,
            queue=runtime_queue,
            skill_registry=registry,
            skill_router=skill_router,
            event_recorder=event_recorder,
        )
        notifier = RuntimeGoalNotifier(sender=self._sender, card_builder=CardBuilder)
        terminal_notifier = AcceptanceGatedNotifier(
            wait_until_accepted=self._runtime_manager.wait_until_accepted,
            notifier=notifier,
        )

        self._runtime_workers = WorkerManager(
            queue=runtime_queue,
            execution_engine=execution_engine,
            worker_count=1,
            terminal_callback=terminal_notifier.notify,
            event_recorder=event_recorder,
        )

        # 调度器：定时任务触发 → 注入 AgentMessageHandler
        async def _schedule_trigger(task_id: str, user_id: str,
                                    chat_id: str, prompt: str) -> None:
            log.info("schedule_firing", task_id=task_id, user_id=user_id[:8])
            # 在 prompt 前加时间戳和任务标识，让模型知道这是定时触发
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            full_prompt = f"[定时任务 #{task_id} · {ts}]\n{prompt}"
            # 用 pro 模型执行定时任务，确保质量
            await self._msg_handler.handle(
                user_id=user_id,
                chat_id=chat_id,
                message_id="",
                text=full_prompt,
                model_override=cfg.MODEL_PRO,
            )

        store = ScheduleStore(cfg.DB_PATH)
        self._scheduler = Scheduler(store=store, trigger_fn=_schedule_trigger)
        self._msg_handler.scheduler = self._scheduler
        self._cmd_handler.scheduler = self._scheduler
        self._cmd_handler.db_log    = self._db_log
        self._cmd_handler.health    = None

        # 健康监控：WS 心跳 + DB 维护 + 资源预警
        async def _health_notify(text: str) -> None:
            # 向所有已知用户发送通知（取最近活跃用户）
            with self._memory._conn() as conn:
                rows = conn.execute(
                    """SELECT DISTINCT user_id FROM messages
                       ORDER BY created_at DESC LIMIT 3"""
                ).fetchall()
            for row in rows:
                chat_id = self._memory.get_profile(
                    row["user_id"], "default_chat_id", ""
                )
                if chat_id:
                    await self._sender.send(chat_id, text=text)
                    break   # 只发给最近活跃的一个用户

        self._health = HealthMonitor(
            db_path=cfg.DB_PATH,
            db_log_handler=self._db_log,
            task_queue=self._queue,
            notify_fn=_health_notify,
        )
        self._cmd_handler.health = self._health

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

            if not is_authorized_user(self.cfg, user_id):
                log.warning("unauthorized_lark_user", user_id=user_id[:8], chat_id=chat_id[:8])
                return

            # WS 心跳：收到消息说明连接正常
            self._health.mark_ws_ok()

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

            # PKB 录入：以 # 开头的消息优先作为个人知识库笔记处理
            note = parse_note_message(text)
            if note:
                content, note_type, topics = note
                pkb_result = await forward_to_pkb_result(content, note_type, topics)
                ok = bool(pkb_result.get("ok"))
                error_detail = pkb_result.get("error") or "请检查 Vercel / Supabase 接口与 API Secret"
                card_fn = getattr(self._msg_handler.card, "pkb_recorded", None)
                if callable(card_fn):
                    await self._sender.send(
                        chat_id,
                        card=card_fn(
                            content=content,
                            note_type=note_type,
                            topics=topics,
                            ok=ok,
                            detail="已转发到个人知识库" if ok else str(error_detail),
                        ),
                        reply_to=message_id,
                    )
                else:
                    if ok:
                        await self._sender.send(
                            chat_id,
                            text=f"✅ 已记录 [{note_type}] 内容",
                            reply_to=message_id,
                        )
                    else:
                        await self._sender.send(
                            chat_id,
                            text="❌ 记录失败，请重试",
                            reply_to=message_id,
                        )
                return

            # 模型前缀解析（/pro /flash /lite 开头，剥离后转 AI）
            model_override = ""
            model_prefixes = {
                "/pro":   self.cfg.MODEL_PRO,
                "/flash": self.cfg.MODEL_FLASH,
                "/lite":  self.cfg.MODEL_LITE,
            }
            for prefix, model in model_prefixes.items():
                if text.lower().startswith(prefix + " ") or text.lower() == prefix:
                    model_override = model
                    text = text[len(prefix):].strip()
                    break

            # 指令优先（大模型无关，且无模型前缀时才走）
            if not model_override:
                handled = await self._cmd_handler.handle(user_id, chat_id, message_id, text)
                if handled:
                    return

            # Goal Runtime gets first refusal for migrated intents.
            runtime_result = await self._runtime_manager.handle_message(
                user_id=user_id,
                chat_id=chat_id,
                text=text,
            )
            if runtime_result["handled"]:
                try:
                    await self._sender.send(
                        chat_id,
                        text=(
                            f"任务已接受：`{runtime_result['goal_id']}`\n"
                            f"{runtime_result['summary']}"
                        ),
                        reply_to=message_id,
                    )
                finally:
                    self._runtime_manager.mark_accepted(
                        runtime_result["goal_id"]
                    )
                return

            # 转给 AI
            asyncio.create_task(
                self._msg_handler.handle(user_id, chat_id, message_id, text,
                                         model_override=model_override)
            )

        except Exception as e:
            log.error("on_message_error", error=str(e))

    # ── 启动 ──────────────────────────────────────────────────────────
    async def _shutdown_components(
        self,
        *,
        ws_runner: LarkWebSocketRunner,
        timeout: float,
    ) -> list[str]:
        components = {
            "websocket": ws_runner.stop,
            "workers": self._runtime_workers.stop,
            "queue": self._queue.stop,
            "scheduler": self._scheduler.stop,
            "health": self._health.stop,
        }
        tasks = {
            name: asyncio.create_task(stop(), name=f"shutdown-{name}")
            for name, stop in components.items()
        }
        _, pending = await asyncio.wait(
            tasks.values(),
            timeout=timeout,
        )
        timed_out = [
            name for name, task in tasks.items()
            if task in pending
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

        for name, task in tasks.items():
            if name in timed_out or task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                log.warning(
                    "shutdown_component_failed",
                    component=name,
                    error_type=type(error).__name__,
                )
        if timed_out:
            log.warning(
                "shutdown_timeout",
                components=timed_out,
                timeout=timeout,
            )
        return timed_out

    async def run(self) -> None:
        log.info("agent_starting")

        # 加载 .env 配置
        self.cfg.load()

        # 初始化组件
        self._init_components()

        # 启动任务队列 + 调度器 + 健康监控
        await self._queue.start()
        await self._scheduler.start()
        await self._health.start()
        await self._runtime_manager.recover_goals()
        self._runtime_workers.start()

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
        from lark_oapi.ws.client import loop as lark_sdk_loop
        ws_runner = LarkWebSocketRunner(
            client=ws_client,
            sdk_loop=lark_sdk_loop,
        )

        # 优雅退出
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

        ws_runner.start()

        log.info("agent_running", mode="websocket",
                 hugo_repo=self.cfg.HUGO_REPO,
                 model_pro=self.cfg.MODEL_PRO)

        await stop_event.wait()

        log.info("shutting_down")
        await self._shutdown_components(
            ws_runner=ws_runner,
            timeout=self.SHUTDOWN_TIMEOUT,
        )

        log.info("agent_stopped")


# ─── 入口 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(AgentApp().run())
