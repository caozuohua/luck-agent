from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from core.agent import MinimalAgent
from core.log import get_logger
from core.router import ToolRouter
from interface.health import HealthService
from interface.lark_ws import LarkWebSocketInterface
from llm.vertex_client import VertexClient
from memory.context_store import ContextStore
from memory.curator import Curator
from memory.db import Database
from memory.goal_store import GoalStore
from memory.pattern_store import PatternStore
from settings import AgentSettings, load_settings
from tools.registry import ToolRegistry

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


class NoopLarkSender:
    async def send_card(self, chat_id: str, card: dict[str, Any]) -> None:
        log.info("lark_card_ready", chat_id=chat_id, message="card prepared")


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
        self.llm_client = VertexClient(
            project=settings.vertex_project,
            location=settings.vertex_location,
            model=settings.vertex_model,
            service_account_key_path=settings.service_account_key_path,
        )
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_builtin_tools()
        self.router = ToolRouter(self.tool_registry)
        self.curator = Curator(
            pattern_store=self.pattern_store,
            llm_client=self.llm_client,
            periodic_interval_seconds=settings.curator_periodic_interval_seconds,
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
        )
        self.lark = LarkWebSocketInterface(agent=self.agent, sender=NoopLarkSender())
        self.health = HealthService(
            db=self.db,
            goal_store=self.goal_store,
            curator=self.curator,
            host=settings.health_host,
            port=settings.health_port,
        )
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        await self.db.initialize()
        recovered = await self.goal_store.get_in_progress("default")
        log.info("goals_recovered", message="in-progress goals recovered", recovered=len(recovered))
        self.router.start_watchdog()
        self.lark.start()
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
