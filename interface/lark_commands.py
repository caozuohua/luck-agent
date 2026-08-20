from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.log import get_logger
from tools.mem0_client import Mem0Client, Mem0SmokeResult
from tools.vps_status import VpsStatusService, format_host_status
from tools.vps_sysops import format_vps_sysops_result
from core.targets import VpsTargetRegistry
from interface.lark_cards import build_target_selection_card

log = get_logger("interface.lark_commands")


class HealthProvider(Protocol):
    async def collect_status(self) -> dict[str, Any]: ...


class VpsSysopsProvider(Protocol):
    async def run(self, operation: str, *, user_id: str = "default") -> Any: ...


@dataclass(frozen=True)
class QuickCommandResult:
    text: str
    card: dict[str, Any] | None = None


class QuickCommandRouter:
    """Handle deterministic Lark commands before the LLM path."""

    def __init__(
        self,
        *,
        health: HealthProvider,
        vps: VpsStatusService,
        sysops: VpsSysopsProvider | None = None,
        mem0: Mem0Client | None = None,
        targets: VpsTargetRegistry | None = None,
    ) -> None:
        self.health = health
        self.vps = vps
        self.sysops = sysops
        self.mem0 = mem0
        self.targets = targets

    async def handle(
        self,
        text: str,
        *,
        user_id: str = "default",
    ) -> str | QuickCommandResult | None:
        raw_command = " ".join(text.strip().split())
        command = raw_command.lower()
        if command in {"/ping", "ping"}:
            return "🏓 pong"
        if command in {"/help", "help", "帮助", "/帮助"}:
            return (
                "可用快捷命令：\n"
                "• `/ping` 连通性\n"
                "• `/health` Bot 与数据库健康状态\n"
                "• `/vps` 当前 VPS 资源状态\n"
                "• `/vps status|resources|services|logs` 运维检查\n"
                "• `/targets` 选择 VPS 目标\n"
                "• `/target TARGET_ID` 切换目标（也可直接使用卡片下拉框）\n"
                "• `/mem0 status` Mem0 API 状态\n"
                "• `/mem0 smoke` Mem0 写入/搜索/清理测试\n"
                "• `/mem0 search 关键词` 搜索记忆"
            )
        if command in {"/targets", "targets", "目标", "/目标", "/target", "target"}:
            return self._targets(user_id)
        for prefix in ("/target ", "target ", "/目标 ", "目标 "):
            if command.startswith(prefix):
                return self._select_target(raw_command[len(prefix) :].strip(), user_id)
        if command in {"/health", "health", "健康", "/健康"}:
            return await self._health()
        if command in {"/vps", "vps", "/status", "status"}:
            return await self._vps(user_id)
        for prefix in ("/vps ", "vps "):
            if command.startswith(prefix):
                operation = command[len(prefix) :].strip()
                if operation in {"status", "resources", "services", "logs"}:
                    return await self._sysops(operation, user_id)
                return None
        if command in {"/mem0 status", "mem0 status"}:
            return await self._mem0_status()
        if command in {"/mem0 smoke", "mem0 smoke"}:
            return await self._mem0_smoke()
        for prefix in ("/mem0 search ", "mem0 search "):
            if command.startswith(prefix):
                query = raw_command[len(prefix) :].strip()
                return await self._mem0_search(query)
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

    def _targets(self, user_id: str) -> QuickCommandResult:
        if self.targets is None:
            return QuickCommandResult("🎯 尚未配置 VPS 目标")
        current = self.targets.current(user_id)
        return QuickCommandResult(
            f"🎯 当前目标：{current.display}",
            build_target_selection_card(self.targets.list(), current=current),
        )

    def _select_target(self, target_id: str, user_id: str) -> QuickCommandResult:
        if self.targets is None:
            return QuickCommandResult("🎯 尚未配置 VPS 目标")
        target = self.targets.select(user_id, target_id)
        if target is None:
            return QuickCommandResult(f"⚠️ 未找到目标：`{target_id}`，请发送 `/targets` 查看列表")
        return self._targets(user_id)

    def select_target(self, target_id: str, user_id: str = "default") -> QuickCommandResult:
        return self._select_target(target_id, user_id)

    async def _vps(self, user_id: str) -> str:
        try:
            if self._is_remote_target(user_id):
                return await self._sysops("resources", user_id)
            try:
                status = await self.vps.collect(user_id=user_id)
            except TypeError as exc:
                if "user_id" not in str(exc):
                    raise
                status = await self.vps.collect()
            return format_host_status(status)
        except Exception as exc:
            log.error("quick_vps_status_failed", error=str(exc))
            return "🖥️ VPS 状态：⚠️ 暂时无法读取主机资源"

    def _is_remote_target(self, user_id: str) -> bool:
        if self.targets is None:
            return False
        selected = self.targets.current(user_id)
        local = getattr(self.vps, "target", None)
        return local is not None and selected.label != local.label

    async def _sysops(self, operation: str, user_id: str) -> str:
        if self.sysops is None:
            return "🖥️ vps_sysops：⚠️ 尚未部署"
        try:
            try:
                result = await self.sysops.run(operation, user_id=user_id)
            except TypeError as exc:
                if "user_id" not in str(exc):
                    raise
                result = await self.sysops.run(operation)
            return format_vps_sysops_result(result)
        except Exception as exc:
            log.error("quick_vps_sysops_failed", operation=operation, error=str(exc))
            return "🖥️ vps_sysops：⚠️ 暂时无法执行检查"

    async def _mem0_status(self) -> str:
        if self.mem0 is None:
            return "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
        try:
            status = await self.mem0.health()
            mark = "✅" if status.ok else "❌"
            detail = f"\n• 说明：{status.detail}" if status.detail else ""
            return f"🧠 Mem0 API：{mark}\n• 延迟：{status.latency_ms} ms{detail}"
        except Exception as exc:
            log.error("quick_mem0_status_failed", error=str(exc))
            return "🧠 Mem0 API：⚠️ 暂时无法读取状态"

    async def _mem0_smoke(self) -> str:
        if self.mem0 is None:
            return "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
        try:
            result: Mem0SmokeResult = await self.mem0.smoke()
            mark = "✅" if result.ok and result.cleanup_confirmed else "⚠️"
            detail = f"\n• 说明：{result.detail}" if result.detail else ""
            return (
                f"🧠 Mem0 smoke：{mark}\n"
                f"• 写入：{result.added}\n"
                f"• 搜索命中：{result.found}\n"
                f"• 清理：{result.deleted}\n"
                f"• 临时标识：`{result.marker}`{detail}"
            )
        except Exception as exc:
            log.error("quick_mem0_smoke_failed", error=str(exc))
            return "🧠 Mem0 smoke：⚠️ 测试失败"

    async def _mem0_search(self, query: str) -> str:
        if not query:
            return "用法：`/mem0 search 关键词`"
        if self.mem0 is None:
            return "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
        try:
            results = await self.mem0.search(query)
            if not results:
                return f"🧠 Mem0 搜索：未找到与“{query}”相关的记忆"
            lines = [f"🧠 Mem0 搜索：{len(results)} 条结果"]
            for index, item in enumerate(results[:5], start=1):
                text = _memory_text(item) or "（无文本）"
                memory_id = str(item.get("id", ""))
                score = item.get("score")
                suffix = f" · {memory_id[:12]}" if memory_id else ""
                if isinstance(score, (int, float)):
                    suffix += f" · score {score:.3f}"
                lines.append(f"{index}. {text[:180]}{suffix}")
            return "\n".join(lines)
        except Exception as exc:
            log.error("quick_mem0_search_failed", error=str(exc))
            return "🧠 Mem0 搜索：⚠️ 查询失败"


def _memory_text(item: dict[str, Any]) -> str:
    for key in ("memory", "text", "content"):
        value = item.get(key)
        if isinstance(value, str):
            return " ".join(value.split())
    return ""
