from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from core.agent import MinimalAgent
from core.log import get_logger
from core.router import ToolRouter
from interface.health import HealthService
from interface.lark_access import LarkAccessPolicy
from interface.lark_approval import LarkApprovalManager
from interface.lark_commands import QuickCommandRouter
from interface.lark_api import LarkApiSender
from interface.lark_sdk import LarkSdkRunner
from interface.web import WebInterface
from llm.fake import FakeLLMClient
from llm.openai_compat import OpenAICompatClient
from memory.context_store import ContextStore
from memory.curator import Curator
from memory.db import Database
from memory.goal_store import GoalStore
from memory.pattern_store import PatternStore
from settings import AgentSettings, load_settings
from tools.registry import ToolRegistry
from tools.mem0_client import Mem0Client
from tools.vps_status import VpsStatusService
from tools.vps_sysops import VpsSysopsAdapter

log = get_logger("main")

INITIALIZATION_SEQUENCE = [
    "load_settings",
    "initialize_sqlite",
    "recover_in_progress_goals",
    "start_lark_websocket",
    "start_health_endpoint",
    "start_curator_periodic_task",
    "register_signal_handlers",
]


class Runtime:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        os.environ.setdefault("SERPER_API_KEY", settings.serper_api_key)
        os.environ.setdefault("AGENT_WORKDIR", settings.agent_workdir)
        os.environ.setdefault("SHELL_TIMEOUT_SECONDS", str(settings.shell_timeout_seconds))
        os.environ.setdefault("SHELL_MAX_OUTPUT_CHARS", str(settings.shell_max_output_chars))
        self.db = Database(settings.db_path)
        self.goal_store = GoalStore(self.db)
        self.pattern_store = PatternStore(self.db)
        self.context_store = ContextStore(self.db)
        # No base url configured -> offline fake client (local dev / CI).
        if settings.llm_base_url:
            self.llm_client: Any = OpenAICompatClient(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
            )
        else:
            self.llm_client = FakeLLMClient(model=settings.llm_model)
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_builtin_tools()
        self.router = ToolRouter(self.tool_registry)
        self.curator = Curator(
            pattern_store=self.pattern_store,
            llm_client=self.llm_client,
            periodic_interval_seconds=settings.curator_periodic_interval_seconds,
        )
        self.lark_access_policy = LarkAccessPolicy.from_csv(
            user_ids=settings.lark_allowed_user_ids,
            chat_ids=settings.lark_allowed_chat_ids,
            allow_unconfigured=settings.lark_allow_unconfigured,
        )
        self.lark_approval_manager = LarkApprovalManager(
            ttl_seconds=settings.lark_approval_ttl_seconds,
        )
        self.agent = MinimalAgent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            router=self.router,
            goal_store=self.goal_store,
            pattern_store=self.pattern_store,
            context_store=self.context_store,
            curator=self.curator,
            curator_trigger_interval=settings.curator_trigger_interval,
            execution_mode=settings.execution_mode,
            max_steps=settings.max_steps,
            max_retry=settings.max_retry,
            graph_db_path=settings.graph_db_path,
            approval_checker=self.lark_approval_manager.consume_grant,
            audit_writer=self.db.insert_operation_audit,
        )
        self.health = HealthService(
            db=self.db,
            goal_store=self.goal_store,
            curator=self.curator,
            host=settings.health_host,
            port=settings.health_port,
        )
        self.vps_status = VpsStatusService(name=settings.vps_name)
        self.vps_sysops = VpsSysopsAdapter(
            root=settings.vps_sysops_root,
            profile=settings.vps_sysops_profile,
            timeout_seconds=settings.vps_sysops_timeout_seconds,
        )
        self.mem0 = (
            Mem0Client(
                base_url=settings.mem0_base_url,
                api_key=settings.mem0_api_key,
                user_id=settings.mem0_user_id,
                agent_id=settings.mem0_agent_id,
                timeout_seconds=settings.mem0_timeout_seconds,
            )
            if settings.mem0_base_url
            else None
        )
        self.quick_commands = QuickCommandRouter(
            health=self.health,
            vps=self.vps_status,
            sysops=self.vps_sysops,
            mem0=self.mem0,
        )
        # Interface: Lark WebSocket when credentials are present, otherwise
        # a local web page for manual testing (no Lark app needed).
        if settings.lark_app_id and settings.lark_app_secret:
            from interface.lark_ws import LarkWebSocketInterface
            import lark_oapi as lark

            self.lark_api_client = (
                lark.Client.builder()
                .app_id(settings.lark_app_id)
                .app_secret(settings.lark_app_secret)
                .domain(settings.lark_domain)
                .build()
            )

            self.lark = LarkWebSocketInterface(
                agent=self.agent,
                sender=LarkApiSender(self.lark_api_client),
                quick_commands=self.quick_commands,
                access_policy=self.lark_access_policy,
                approval_manager=self.lark_approval_manager,
            )
            self.lark_runner: LarkSdkRunner | None = None
        else:
            self.lark = WebInterface(
                agent=self.agent,
                host=settings.web_host,
                port=settings.web_port,
            )
            self.lark_runner = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        await self.db.initialize()
        recovered = await self.goal_store.get_in_progress("default")
        log.info("goals_recovered", message="in-progress goals recovered", recovered=len(recovered))
        self.router.start_watchdog()
        self.lark.start()
        if self.lark_runner is None and self.settings.lark_app_id and self.settings.lark_app_secret:
            from interface.lark_ws import LarkWebSocketInterface

            if isinstance(self.lark, LarkWebSocketInterface):
                loop = asyncio.get_running_loop()
                self.lark_runner = LarkSdkRunner(
                    app_id=self.settings.lark_app_id,
                    app_secret=self.settings.lark_app_secret,
                    domain=self.settings.lark_domain,
                    application_loop=loop,
                    on_message=self.lark.handle_message,
                    on_connected=self.lark.mark_heartbeat,
                    on_disconnected=lambda: setattr(self.lark, "connected", False),
                    stop_timeout=self.settings.shutdown_timeout_seconds,
                )
                self.lark_runner.start()
        await self.health.start()
        self.curator.start_periodic()
        self._register_signal_handlers()
        log.info("runtime_started", message="runtime started")

    async def wait(self) -> None:
        await self._stop_event.wait()

    async def stop(self) -> None:
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self.lark.drain_active(self.settings.shutdown_timeout_seconds),
                    self.agent.drain_background_tasks(),
                ),
                timeout=self.settings.shutdown_timeout_seconds,
            )
        except TimeoutError:
            log.warning("shutdown_timeout", message="forced shutdown after timeout")
        await self.router.stop_watchdog()
        if self.lark_runner is not None:
            await self.lark_runner.stop()
        await self.lark.stop()
        await self.curator.stop_periodic()
        await self.health.stop()
        await self.db.close()
        log.info("runtime_stopped", message="runtime stopped")

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: self._stop_event.set())


async def async_main() -> None:
    settings = load_settings()
    runtime = Runtime(settings)
    await runtime.start()
    try:
        await runtime.wait()
    finally:
        await runtime.stop()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
