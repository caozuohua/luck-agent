from __future__ import annotations

from typing import Any, Protocol

from core.log import get_logger
from tools.vps_status import VpsStatusService, format_host_status

log = get_logger("interface.lark_commands")


class HealthProvider(Protocol):
    async def collect_status(self) -> dict[str, Any]: ...


class QuickCommandRouter:
    """Handle deterministic Lark commands before the LLM path."""

    def __init__(
        self,
        *,
        health: HealthProvider,
        vps: VpsStatusService,
    ) -> None:
        self.health = health
        self.vps = vps

    async def handle(self, text: str, *, user_id: str = "default") -> str | None:
        command = " ".join(text.strip().lower().split())
        if command in {"/ping", "ping"}:
            return "🏓 pong"
        if command in {"/help", "help", "帮助", "/帮助"}:
            return (
                "可用快捷命令：\n"
                "• `/ping` 连通性\n"
                "• `/health` Bot 与数据库健康状态\n"
                "• `/vps` 当前 VPS 资源状态"
            )
        if command in {"/health", "health", "健康", "/健康"}:
            return await self._health()
        if command in {
            "/vps",
            "vps",
            "/status",
            "status",
            "/vps status",
            "vps status",
        }:
            return await self._vps()
        return None

    async def _health(self) -> str:
        try:
            status = await self.health.collect_status()
            process_ok = status.get("process", {}).get("status") == "ok"
            sqlite_ok = bool(status.get("sqlite", {}).get("connected"))
            goals = status.get("goals", {})
            done = int(goals.get("done", 0))
            failed = int(goals.get("failed", 0))
            sqlite_mark = "✅" if sqlite_ok else "❌"
            process_mark = "✅" if process_ok else "❌"
            return (
                f"🩺 Luck Agent 健康：{process_mark}\n"
                f"• SQLite：{sqlite_mark}\n"
                f"• 目标：完成 {done}，失败 {failed}"
            )
        except Exception as exc:
            log.error("quick_health_failed", error=str(exc))
            return "🩺 Luck Agent 健康：⚠️ 暂时无法读取状态"

    async def _vps(self) -> str:
        try:
            return format_host_status(await self.vps.collect())
        except Exception as exc:
            log.error("quick_vps_status_failed", error=str(exc))
            return "🖥️ VPS 状态：⚠️ 暂时无法读取主机资源"
