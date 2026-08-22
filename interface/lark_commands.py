from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from core.log import get_logger
from core.operation_policy import OperationPermissionPolicy
from core.services import SERVICE_CATALOG, format_service_catalog, get_service
from tools.mem0_client import Mem0Client, Mem0SmokeResult
from tools.vps_status import VpsStatusService, format_host_status
from tools.vps_sysops import format_vps_sysops_result
from core.targets import VpsTargetRegistry
from interface.lark_cards import (
    build_log_page_card,
    build_output_page_card,
    build_sections_card,
    build_service_catalog_card,
    build_target_selection_card,
)
from memory.scope_store import MemoryScopeStore
from memory.target_store import TargetSelectionStore

log = get_logger("interface.lark_commands")

ApprovalChecker = Callable[[str, str, str, dict[str, Any]], bool]
AuditWriter = Callable[..., Awaitable[Any]]


class HealthProvider(Protocol):
    async def collect_status(self) -> dict[str, Any]: ...


class VpsSysopsProvider(Protocol):
    async def run(self, operation: str, *, user_id: str = "default") -> Any: ...

    async def probe_service(self, service: str, *, user_id: str = "default") -> Any: ...


class ServiceHealthProvider(Protocol):
    async def health(self) -> Any: ...


@dataclass(frozen=True)
class QuickCommandResult:
    text: str
    card: dict[str, Any] | None = None


@dataclass(frozen=True)
class _LogPageSession:
    result: Any
    expires_at: float


class QuickCommandRouter:
    """Handle deterministic Lark commands before the LLM path."""

    def __init__(
        self,
        *,
        health: HealthProvider,
        vps: VpsStatusService,
        sysops: VpsSysopsProvider | None = None,
        mem0: Mem0Client | None = None,
        scope_store: MemoryScopeStore | None = None,
        targets: VpsTargetRegistry | None = None,
        target_store: TargetSelectionStore | None = None,
        permission_policy: OperationPermissionPolicy | None = None,
        mem0_target_id: str = "",
        new_api: ServiceHealthProvider | None = None,
        agent_target_id: str = "",
        new_api_target_id: str = "",
        approval_checker: ApprovalChecker | None = None,
        audit_writer: AuditWriter | None = None,
    ) -> None:
        self.health = health
        self.vps = vps
        self.sysops = sysops
        self.mem0 = mem0
        self.scope_store = scope_store
        self.targets = targets
        self.target_store = target_store
        self.permission_policy = permission_policy
        self.mem0_target_id = mem0_target_id.strip().lower()
        self.new_api = new_api
        self.agent_target_id = agent_target_id.strip().lower()
        self.new_api_target_id = new_api_target_id.strip().lower()
        self.approval_checker = approval_checker
        self.audit_writer = audit_writer
        self._log_page_sessions: dict[tuple[str, str], _LogPageSession] = {}
        self._pending_target_selections: dict[tuple[str, str], str] = {}
        self._log_page_ttl_seconds = 10 * 60
        self._log_page_max_sessions = 128

    async def handle(
        self,
        text: str,
        *,
        user_id: str = "default",
        chat_id: str = "",
        approval_token: str | None = None,
    ) -> str | QuickCommandResult | None:
        raw_command = " ".join(text.strip().split())
        command = raw_command.lower()
        await self._restore_target_selection(user_id, chat_id)
        if command in {"/ping", "ping"}:
            return "🏓 pong"
        if command in {"/help", "help", "帮助", "/帮助"}:
            return (
                "可用快捷命令：\n"
                "• `/ping` 连通性\n"
                "• `/health` Bot 与数据库健康状态\n"
                "• `/vps` 当前 VPS 资源状态\n"
                "• `/vps status|resources|services|logs` 运维检查（日志支持卡片翻页）\n"
                "• `/vps service list` 服务目录\n"
                "• `/vps service mem0 status|list|smoke|search 关键词` Mem0 服务操作\n"
                "• `/vps service luck-agent restart` 重启 Agent（需确认）\n"
                "• `/targets` 选择 VPS 目标\n"
                "• `/target TARGET_ID` 切换目标（也可直接使用卡片下拉框）\n"
                "• `/mem0 status` Mem0 API 状态\n"
                "• `/mem0 scope [PROJECT_ID]` 查看或切换当前项目 scope\n"
                "• `/mem0 list` 浏览当前 scope 的记忆\n"
                "• `/mem0 smoke` Mem0 写入/搜索/清理测试\n"
                "• `/mem0 search 关键词` 搜索记忆\n"
                "• `/mem0 save 内容` 保存记忆（需确认）\n"
                "• `/mem0 delete MEMORY_ID` 删除记忆（需确认）"
            )
        if command in {"/targets", "targets", "目标", "/目标", "/target", "target"}:
            return self._targets(user_id)
        for prefix in ("/target ", "target ", "/目标 ", "目标 "):
            if command.startswith(prefix):
                return self._select_target(
                    raw_command[len(prefix) :].strip(),
                    user_id,
                    chat_id=chat_id,
                )
        if command in {"/health", "health", "健康", "/健康"}:
            return await self._health()
        if command in {"/vps", "vps", "/status", "status"}:
            return await self._vps(user_id)
        for prefix in ("/vps service ", "vps service ", "/service ", "service "):
            if command.startswith(prefix):
                return await self._service(
                    raw_command[len(prefix) :].strip(),
                    user_id,
                    chat_id=chat_id,
                    approval_token=approval_token,
                )
        if command in {"/vps service", "vps service", "/service", "service"}:
            return self._service_catalog()
        for prefix in ("/vps ", "vps "):
            if command.startswith(prefix):
                operation = command[len(prefix) :].strip()
                if operation in {"status", "resources", "services", "logs"}:
                    return await self._sysops(operation, user_id)
                return None
        if command in {"/mem0 status", "mem0 status"}:
            return await self._mem0_status(user_id=user_id, chat_id=chat_id)
        if command in {"/mem0 scope", "mem0 scope", "/mem0 project", "mem0 project"}:
            return await self._mem0_scope(user_id=user_id, chat_id=chat_id)
        for prefix in ("/mem0 scope ", "mem0 scope ", "/mem0 project ", "mem0 project "):
            if command.startswith(prefix):
                project_id = raw_command[len(prefix) :].strip()
                return await self._mem0_scope(
                    user_id=user_id,
                    chat_id=chat_id,
                    project_id=project_id,
                )
        if command in {"/mem0 list", "mem0 list", "/mem0 memories", "mem0 memories"}:
            return await self._mem0_list(user_id=user_id, chat_id=chat_id)
        if command in {"/mem0 smoke", "mem0 smoke"}:
            return await self._mem0_smoke(user_id=user_id, chat_id=chat_id)
        for prefix in ("/mem0 search ", "mem0 search "):
            if command.startswith(prefix):
                query = raw_command[len(prefix) :].strip()
                return await self._mem0_search(query, user_id=user_id, chat_id=chat_id)
        for prefix in (
            "/mem0 save ",
            "mem0 save ",
            "/mem0 remember ",
            "mem0 remember ",
        ):
            if command.startswith(prefix):
                content = raw_command[len(prefix) :].strip()
                return await self._mem0_save(
                    content,
                    user_id=user_id,
                    chat_id=chat_id,
                    approval_token=approval_token,
                )
        for prefix in ("/mem0 delete ", "mem0 delete "):
            if command.startswith(prefix):
                memory_id = raw_command[len(prefix) :].strip().strip("`")
                return await self._mem0_delete(
                    memory_id,
                    user_id=user_id,
                    chat_id=chat_id,
                    approval_token=approval_token,
                )
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
            lines = [
                f"🩺 Luck Agent 健康：{process_mark}",
                f"• SQLite：{sqlite_mark}",
                f"• 目标：完成 {done}，失败 {failed}",
            ]
            llm = status.get("llm") or {}
            providers = llm.get("providers") or []
            active_provider = str(llm.get("active_provider") or "")
            if providers and active_provider:
                ready = sum(1 for item in providers if item.get("state") == "ready")
                active = next(
                    (item for item in providers if item.get("provider") == active_provider),
                    {},
                )
                active_state = str(active.get("state") or "unknown")
                active_mark = "✅" if active_state == "ready" else "⚠️"
                detail = ""
                if active_state == "cooldown":
                    detail = (
                        f"，冷却 {active.get('cooldown_remaining_seconds', 0)} 秒"
                        f"（{active.get('cooldown_kind') or 'temporary'}）"
                    )
                lines.append(
                    f"• LLM：{active_mark} {active_provider}"
                    f"（可用 {ready}/{len(providers)}{detail}）"
                )
            return "\n".join(lines)
        except Exception as exc:
            log.error("quick_health_failed", error=str(exc))
            return "🩺 Luck Agent 健康：⚠️ 暂时无法读取状态"

    def _targets(self, user_id: str) -> QuickCommandResult:
        if self.targets is None:
            return QuickCommandResult("🎯 尚未配置 VPS 目标")
        if self.permission_policy is not None and not self.permission_policy.allows_user(user_id):
            return QuickCommandResult("⛔ 当前用户无权执行 VPS 运维操作")
        current = self.targets.current(user_id)
        allowed_targets = [
            target
            for target in self.targets.list()
            if self._target_allowed(target.label, user_id)
        ]
        if not allowed_targets:
            return QuickCommandResult("🎯 当前用户没有已授权的 VPS 目标")
        current_for_card = current if self._target_allowed(current.label, user_id) else None
        current_text = (
            current_for_card.display if current_for_card is not None else "未选择已授权目标"
        )
        return QuickCommandResult(
            f"🎯 当前目标：{current_text}",
            build_target_selection_card(allowed_targets, current=current_for_card),
        )

    def _select_target(
        self,
        target_id: str,
        user_id: str,
        *,
        chat_id: str = "",
    ) -> QuickCommandResult:
        if self.targets is None:
            return QuickCommandResult("🎯 尚未配置 VPS 目标")
        target = next(
            (item for item in self.targets.list() if item.label.lower() == target_id.lower()),
            None,
        )
        if target is None:
            return QuickCommandResult(f"⚠️ 未找到目标：`{target_id}`，请发送 `/targets` 查看列表")
        if not self._target_allowed(target.label, user_id):
            return QuickCommandResult(f"⛔ 当前用户无权访问目标：`{target.label}`")
        self.targets.select(user_id, target.label)
        self._pending_target_selections[(str(user_id or "default"), str(chat_id or ""))] = target.label
        return self._targets(user_id)

    def select_target(
        self,
        target_id: str,
        user_id: str = "default",
        *,
        chat_id: str = "",
    ) -> QuickCommandResult:
        return self._select_target(target_id, user_id, chat_id=chat_id)

    def current_target_label(self, user_id: str = "default") -> str:
        if self.targets is None:
            return ""
        return self.targets.current(user_id).label

    async def _restore_target_selection(self, user_id: str, chat_id: str) -> None:
        if self.targets is None or self.target_store is None:
            return
        key = (str(user_id or "default"), str(chat_id or ""))
        pending = self._pending_target_selections.pop(key, None)
        if pending:
            await self.target_store.set(key[0], key[1], pending)
        selected = await self.target_store.get(key[0], key[1])
        if selected and self.targets.select(key[0], selected) is None:
            log.warning("target_selection_restore_skipped", user_id=key[0], target_id=selected)

    async def _vps(self, user_id: str) -> str:
        try:
            denied = self._target_denial(user_id)
            if denied:
                return denied
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
        if selected.ssh_host and local is None:
            return True
        return local is not None and selected.label != local.label

    async def _sysops(self, operation: str, user_id: str) -> str | QuickCommandResult:
        if self.sysops is None:
            return "🖥️ vps_sysops：⚠️ 尚未部署"
        try:
            denied = self._target_denial(user_id)
            if denied:
                return denied
            try:
                result = await self.sysops.run(operation, user_id=user_id)
            except TypeError as exc:
                if "user_id" not in str(exc):
                    raise
                result = await self.sysops.run(operation)
            return self._start_output_pagination(result, user_id)
        except Exception as exc:
            log.error("quick_vps_sysops_failed", operation=operation, error=str(exc))
            return "🖥️ vps_sysops：⚠️ 暂时无法执行检查"

    def render_log_page(
        self,
        token: str,
        page: int,
        *,
        user_id: str = "default",
    ) -> QuickCommandResult:
        """Render one cached log page for the owning Lark user."""
        return self.render_output_page(token, page, user_id=user_id)

    def render_output_page(
        self,
        token: str,
        page: int,
        *,
        user_id: str = "default",
    ) -> QuickCommandResult:
        """Render one cached command-output page for its owning Lark user."""
        self._purge_log_page_sessions()
        key = (user_id, token.strip())
        session = self._log_page_sessions.get(key)
        if session is None:
            return QuickCommandResult("⚠️ 日志分页已过期，请重新发送 `/vps logs`")
        pages = tuple(getattr(session.result, "output_pages", ()) or ())
        if not pages:
            return QuickCommandResult(format_vps_sysops_result(session.result))
        if page < 1 or page > len(pages):
            return QuickCommandResult("⚠️ 输出页码无效，请重新发送原命令")
        result = replace(session.result, output=pages[page - 1])
        label = "日志" if result.operation == "logs" else "输出"
        text = format_vps_sysops_result(result).replace(
            f"📄 {label}第 1/{len(pages)} 页",
            f"📄 {label}第 {page}/{len(pages)} 页",
        )
        if result.operation == "logs":
            card = build_log_page_card(
                text,
                page=page,
                total_pages=len(pages),
                token=token,
            )
        else:
            card = build_output_page_card(
                text,
                page=page,
                total_pages=len(pages),
                token=token,
                heading="服务输出",
            )
        return QuickCommandResult(
            text,
            card,
        )

    def _start_output_pagination(self, result: Any, user_id: str) -> str | QuickCommandResult:
        pages = tuple(getattr(result, "output_pages", ()) or ())
        if len(pages) <= 1:
            return format_vps_sysops_result(result)
        self._purge_log_page_sessions()
        token = secrets.token_urlsafe(9)
        self._log_page_sessions[(user_id, token)] = _LogPageSession(
            result=result,
            expires_at=time.time() + self._log_page_ttl_seconds,
        )
        while len(self._log_page_sessions) > self._log_page_max_sessions:
            oldest = next(iter(self._log_page_sessions))
            self._log_page_sessions.pop(oldest, None)
        first = self.render_log_page(token, 1, user_id=user_id)
        return first

    def _purge_log_page_sessions(self) -> None:
        now = time.time()
        for key, session in list(self._log_page_sessions.items()):
            if session.expires_at <= now:
                self._log_page_sessions.pop(key, None)

    def _target_allowed(self, target_id: str, user_id: str = "") -> bool:
        return self.permission_policy is None or (
            self.permission_policy.allows_user(user_id)
            and self.permission_policy.allows_target(target_id)
        )

    def _target_denial(self, user_id: str) -> str | None:
        if self.permission_policy is not None and not self.permission_policy.allows_user(user_id):
            return "⛔ 当前用户无权执行 VPS 运维操作"
        if self.targets is None:
            return None
        target = self.targets.current(user_id)
        if self._target_allowed(target.label, user_id):
            return None
        return f"⛔ 当前用户无权访问目标：`{target.label}`"

    def _service_catalog(self) -> QuickCommandResult:
        allowed = None
        if self.permission_policy is not None and self.permission_policy.allowed_services:
            allowed = self.permission_policy.allowed_services
        specs = [
            spec
            for spec in SERVICE_CATALOG
            if allowed is None or spec.service_id in allowed
        ]
        return QuickCommandResult(
            format_service_catalog(allowed=allowed),
            build_service_catalog_card(specs),
        )

    async def _service(
        self,
        request: str,
        user_id: str,
        *,
        chat_id: str = "",
        approval_token: str | None = None,
    ) -> str | QuickCommandResult:
        parts = request.split(maxsplit=2)
        service_id = parts[0].lower() if parts else ""
        if self.permission_policy is not None and not self.permission_policy.allows_user(user_id):
            return "⛔ 当前用户无权执行 VPS 运维操作"
        if service_id in {"list", "catalog", "help"}:
            return self._service_catalog()
        spec = get_service(service_id)
        if spec is None:
            return f"🧩 未登记服务：`{service_id or '(empty)'}`\n{self._service_catalog()}"
        if self.permission_policy is not None and not self.permission_policy.allows_service(spec.service_id):
            return f"⛔ 当前用户无权访问服务：`{spec.service_id}`"
        denied = self._target_denial(user_id)
        if denied:
            return denied

        action = parts[1].lower() if len(parts) > 1 else "status"
        argument = parts[2].strip() if len(parts) > 2 else ""
        if action == "restart":
            if not spec.restartable:
                return f"⚠️ 服务 `{spec.service_id}` 当前不开放重启操作"
            if (
                spec.service_id == "new-api"
                and self.new_api_target_id
                and self.targets is not None
                and self.targets.current(user_id).label.lower() != self.new_api_target_id
            ):
                return (
                    f"🧩 new-api 当前绑定目标为 `{self.new_api_target_id}`；"
                    f"当前选择为 `{self.targets.current(user_id).label}`，请先切换目标"
                )
            if self.permission_policy is not None and not self.permission_policy.allows_operation(
                "restart"
            ):
                return "⛔ 当前用户无权执行操作：`restart`"
            return await self._restart_service(
                spec.service_id,
                user_id,
                approval_token=approval_token,
            )
        if spec.backend == "mem0":
            if self.mem0_target_id and self.targets is not None:
                target = self.targets.current(user_id)
                if target.label.lower() != self.mem0_target_id:
                    return (
                        f"🧩 {spec.label} 当前绑定目标为 `{self.mem0_target_id}`；"
                        f"当前选择为 `{target.label}`，请先切换目标"
                    )
            if action in {"status", "health"}:
                return await self._mem0_status(user_id=user_id, chat_id=chat_id)
            if action == "smoke":
                return await self._mem0_smoke(user_id=user_id, chat_id=chat_id)
            if action == "list":
                return await self._mem0_list(user_id=user_id, chat_id=chat_id)
            if action == "search":
                return await self._mem0_search(
                    argument,
                    user_id=user_id,
                    chat_id=chat_id,
                )
            return "用法：`/vps service mem0 status|list|smoke|search 关键词`"

        if spec.backend == "http":
            if action not in {"status", "health"}:
                return f"用法：`/vps service {spec.service_id} status`"
            return await self._new_api_status()

        if spec.backend == "probe":
            if action not in {"status", "health"}:
                return f"用法：`/vps service {spec.service_id} status`"
            return await self._service_probe(spec.service_id, user_id)

        if action not in {"status", "health", "list"}:
            return f"用法：`/vps service {spec.service_id} status`"
        result = await self._sysops("services", user_id)
        return _prefix_result(f"🧩 {spec.label} · 宿主机服务清单", result)

    async def _new_api_status(self) -> str | QuickCommandResult:
        if self.new_api is None:
            text = "🤖 new-api：⚠️ 未配置 LLM_BASE_URL"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · new-api"))
        try:
            status = await self.new_api.health()
            mark = "✅" if status.ok else "❌"
            detail = f"\n• 说明：{status.detail}" if status.detail else ""
            text = f"🤖 new-api API：{mark}\n• 延迟：{status.latency_ms} ms{detail}"
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · new-api"),
            )
        except Exception as exc:
            log.error("quick_new_api_status_failed", error=str(exc))
            text = "🤖 new-api API：⚠️ 暂时无法读取状态"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · new-api"))

    async def _service_probe(self, service: str, user_id: str) -> str | QuickCommandResult:
        probe = getattr(self.sysops, "probe_service", None)
        if not callable(probe):
            result = await self._sysops("services", user_id)
            return _prefix_result(f"🧩 {service} · 宿主机服务清单", result)
        try:
            try:
                result = await probe(service, user_id=user_id)
            except TypeError as exc:
                if "user_id" not in str(exc):
                    raise
                result = await probe(service)
            text = _format_service_probe(service, result)
            return QuickCommandResult(
                text,
                build_sections_card([text], title=f"Luck Agent · {service} 探针"),
            )
        except Exception as exc:
            log.error("quick_service_probe_failed", service=service, error=str(exc))
            text = f"🧩 {service}：⚠️ 服务探针失败"
            return QuickCommandResult(
                text,
                build_sections_card([text], title=f"Luck Agent · {service} 探针"),
            )

    async def _restart_service(
        self,
        service: str,
        user_id: str,
        *,
        approval_token: str | None,
    ) -> str:
        restart = getattr(self.sysops, "restart_service", None)
        if not callable(restart):
            return "⚠️ 当前 vps_sysops 未提供固定重启入口"
        if not approval_token or self.approval_checker is None:
            return "⚠️ 重启操作必须先完成一次性确认"
        target_id = self.targets.current(user_id).label if self.targets is not None else ""
        if service == "luck-agent" and self.agent_target_id and target_id.lower() != self.agent_target_id:
            return (
                f"🧩 Luck Agent 当前绑定目标为 `{self.agent_target_id}`；"
                f"当前选择为 `{target_id}`，请先切换目标"
            )
        args = {"target": target_id, "service": service, "operation": "restart"}
        try:
            approved = self.approval_checker(
                user_id,
                approval_token,
                "service_restart",
                args,
            )
        except Exception:
            approved = False
        await self._audit_service_operation(
            user_id=user_id,
            service=service,
            target=target_id,
            decision="approved" if approved else "denied",
            details="approval_token_present=true",
        )
        if not approved:
            return "⛔ 重启确认码无效、过期或与目标/服务不匹配"
        try:
            try:
                result = await restart(service, user_id=user_id)
            except TypeError as exc:
                if "user_id" not in str(exc):
                    raise
                result = await restart(service)
        except Exception as exc:
            await self._audit_service_operation(
                user_id=user_id,
                service=service,
                target=target_id,
                decision="executed",
                details=f"status=error error={str(exc)[:240]}",
            )
            return "⚠️ 服务重启执行失败"
        await self._audit_service_operation(
            user_id=user_id,
            service=service,
            target=target_id,
            decision="executed",
            details=f"status={'ok' if getattr(result, 'ok', False) else 'error'}",
        )
        return self._start_output_pagination(result, user_id)

    async def _audit_service_operation(
        self,
        *,
        user_id: str,
        service: str,
        target: str,
        decision: str,
        details: str,
    ) -> None:
        if self.audit_writer is None:
            return
        try:
            await self.audit_writer(
                user_id=user_id,
                tool_name="service_restart",
                operation=f"restart service={service} target={target}",
                decision=decision,
                details=details,
            )
        except Exception:
            log.warning("quick_service_audit_failed", service=service, target=target)

    async def _mem0_scope(
        self,
        *,
        user_id: str,
        chat_id: str,
        project_id: str = "",
    ) -> str | QuickCommandResult:
        if self.mem0 is None:
            text = "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · Mem0"))
        projects = tuple(getattr(self.mem0, "project_ids", ()) or ())
        default_project = str(getattr(self.mem0, "agent_id", "luck-agent"))
        if not projects:
            projects = (default_project,)
        current = await self._current_mem0_project(user_id, chat_id)
        if project_id:
            if project_id not in projects:
                text = (
                    f"🧠 Mem0 项目 scope：⚠️ 未授权项目 `{project_id}`\n"
                    f"• 可选：{', '.join(f'`{item}`' for item in projects)}\n"
                    f"• 当前：`{current}`"
                )
                return QuickCommandResult(
                    text,
                    build_sections_card([text], title="Luck Agent · Mem0 Scope"),
                )
            if self.scope_store is None:
                text = "🧠 Mem0 项目 scope：⚠️ 当前运行时未启用持久化选择"
                return QuickCommandResult(
                    text,
                    build_sections_card([text], title="Luck Agent · Mem0 Scope"),
                )
            await self.scope_store.set(user_id, chat_id, project_id)
            current = project_id
            text = f"🧠 Mem0 项目 scope 已切换：✅\n• 当前：`{current}`\n• 会话：按当前 Lark 会话保存"
        else:
            text = (
                f"🧠 Mem0 项目 scope：`{current}`\n"
                f"• 可选：{', '.join(f'`{item}`' for item in projects)}\n"
                "• 切换：`/mem0 scope PROJECT_ID`\n"
                "• 该选择按用户 + 会话保存；临时上下文不会写入 Mem0"
            )
        return QuickCommandResult(
            text,
            build_sections_card([text], title="Luck Agent · Mem0 Scope"),
        )

    async def _current_mem0_project(self, user_id: str, chat_id: str) -> str:
        default_project = str(getattr(self.mem0, "agent_id", "luck-agent"))
        if self.scope_store is None:
            return default_project
        selected = await self.scope_store.get(user_id, chat_id)
        projects = set(getattr(self.mem0, "project_ids", ()) or ())
        return selected if selected in projects else default_project

    def _mem0_project_kwargs(self, project_id: str) -> dict[str, str]:
        default_project = str(getattr(self.mem0, "agent_id", "luck-agent"))
        return {} if project_id == default_project else {"project_id": project_id}

    def _mem0_scope_label(self, user_id: str, project_id: str) -> str:
        try:
            return self.mem0.scope_label(user_id, project_id=project_id)
        except TypeError:
            # Keep lightweight test doubles and older senders compatible.
            return self.mem0.scope_label(user_id)

    async def _mem0_status(
        self,
        *,
        user_id: str = "default",
        chat_id: str = "",
    ) -> str | QuickCommandResult:
        if self.mem0 is None:
            text = "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · Mem0"))
        try:
            status = await self.mem0.health()
            project_id = await self._current_mem0_project(user_id, chat_id)
            mark = "✅" if status.ok else "❌"
            detail = f"\n• 说明：{status.detail}" if status.detail else ""
            text = (
                f"🧠 Mem0 API：{mark}\n"
                f"• 延迟：{status.latency_ms} ms\n"
                f"• Scope：{self._mem0_scope_label(user_id, project_id)}{detail}"
            )
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0"),
            )
        except Exception as exc:
            log.error("quick_mem0_status_failed", error=str(exc))
            text = "🧠 Mem0 API：⚠️ 暂时无法读取状态"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · Mem0"))

    async def _mem0_smoke(
        self,
        *,
        user_id: str = "default",
        chat_id: str = "",
    ) -> str | QuickCommandResult:
        if self.mem0 is None:
            text = "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · Mem0 smoke"))
        try:
            project_id = await self._current_mem0_project(user_id, chat_id)
            result: Mem0SmokeResult = await self.mem0.smoke(
                actor_id=user_id,
                **self._mem0_project_kwargs(project_id),
            )
            mark = "✅" if result.ok and result.cleanup_confirmed else "⚠️"
            detail = f"\n• 说明：{result.detail}" if result.detail else ""
            text = (
                f"🧠 Mem0 smoke：{mark}\n"
                f"• 写入：{result.added}\n"
                f"• 搜索命中：{result.found}\n"
                f"• 清理：{result.deleted}\n"
                f"• 临时标识：`{result.marker}`\n"
                f"• Scope：{self._mem0_scope_label(user_id, project_id)}{detail}"
            )
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0 smoke"),
            )
        except Exception as exc:
            log.error("quick_mem0_smoke_failed", error=str(exc))
            text = "🧠 Mem0 smoke：⚠️ 测试失败"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · Mem0 smoke"))

    async def _mem0_search(
        self,
        query: str,
        *,
        user_id: str = "default",
        chat_id: str = "",
    ) -> str | QuickCommandResult:
        if not query:
            return "用法：`/mem0 search 关键词`"
        if self.mem0 is None:
            text = "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · Mem0"))
        try:
            project_id = await self._current_mem0_project(user_id, chat_id)
            results = await self.mem0.search(
                query,
                actor_id=user_id,
                **self._mem0_project_kwargs(project_id),
            )
            if not results:
                text = (
                    f"🧠 Mem0 搜索：未找到与“{query}”相关的记忆\n"
                    f"• Scope：{self._mem0_scope_label(user_id, project_id)}"
                )
                return QuickCommandResult(
                    text,
                    build_sections_card([text], title="Luck Agent · Mem0 搜索"),
                )
            lines = [
                f"🧠 Mem0 搜索：{len(results)} 条结果",
                f"• Scope：{self._mem0_scope_label(user_id, project_id)}",
            ]
            sections = list(lines)
            for index, item in enumerate(results[:5], start=1):
                text = _memory_text(item) or "（无文本）"
                memory_id = str(item.get("id", ""))
                score = item.get("score")
                suffix = f" · {memory_id[:12]}" if memory_id else ""
                if isinstance(score, (int, float)):
                    suffix += f" · score {score:.3f}"
                line = f"{index}. {text[:180]}{suffix}"
                lines.append(line)
                sections.append(line)
            return QuickCommandResult(
                "\n".join(lines),
                build_sections_card(sections, title="Luck Agent · Mem0 搜索"),
            )
        except Exception as exc:
            log.error("quick_mem0_search_failed", error=str(exc))
            text = "🧠 Mem0 搜索：⚠️ 查询失败"
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0 搜索"),
            )

    async def _mem0_list(
        self,
        *,
        user_id: str = "default",
        chat_id: str = "",
    ) -> str | QuickCommandResult:
        if self.mem0 is None:
            text = "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
            return QuickCommandResult(text, build_sections_card([text], title="Luck Agent · Mem0"))
        try:
            project_id = await self._current_mem0_project(user_id, chat_id)
            results = await self.mem0.list_memories(
                limit=10,
                actor_id=user_id,
                **self._mem0_project_kwargs(project_id),
            )
            scope = self._mem0_scope_label(user_id, project_id)
            if not results:
                text = f"🧠 Mem0 记忆清单：当前没有记忆\n• Scope：{scope}"
                return QuickCommandResult(
                    text,
                    build_sections_card([text], title="Luck Agent · Mem0 清单"),
                )
            lines = [f"🧠 Mem0 记忆清单：{len(results)} 条（最多显示 10 条）", f"• Scope：{scope}"]
            sections = list(lines)
            for index, item in enumerate(results[:10], start=1):
                memory_id = str(item.get("id", ""))
                memory = _memory_text(item) or "（无文本）"
                line = f"{index}. {memory[:180]}"
                if memory_id:
                    line += f"\n   ID：`{memory_id[:160]}`"
                lines.append(line)
                sections.append(line)
            text = "\n".join(lines)
            return QuickCommandResult(
                text,
                build_sections_card(sections, title="Luck Agent · Mem0 清单"),
            )
        except Exception as exc:
            log.error("quick_mem0_list_failed", error=type(exc).__name__)
            text = "🧠 Mem0 记忆清单：⚠️ 查询失败"
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0 清单"),
            )

    async def _mem0_save(
        self,
        content: str,
        *,
        user_id: str,
        chat_id: str,
        approval_token: str | None,
    ) -> str | QuickCommandResult:
        if not content:
            return "用法：`/mem0 save 要保存的内容`"
        if len(content) > 4000:
            return "⚠️ 记忆内容过长，请控制在 4000 字符以内"
        if self.mem0 is None:
            return "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
        denied = self._memory_write_denial(
            user_id=user_id,
            approval_token=approval_token,
            operation="write",
            tool_name="memory_write",
        )
        if denied:
            return denied
        try:
            project_id = await self._current_mem0_project(user_id, chat_id)
            payload = await self.mem0.add(
                content,
                metadata={"source": "lark-explicit", "user_confirmed": True},
                actor_id=user_id,
                **self._mem0_project_kwargs(project_id),
            )
            added = _memory_result_count(payload)
            text = (
                f"🧠 Mem0 记忆已保存：✅\n"
                f"• 内容长度：{len(content)}\n• 写入条目：{added}\n"
                f"• Scope：{self._mem0_scope_label(user_id, project_id)}"
            )
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0 保存"),
            )
        except Exception as exc:
            log.error("quick_mem0_save_failed", error=type(exc).__name__)
            text = "🧠 Mem0 记忆保存：⚠️ 服务不可用，未阻塞其他任务"
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0 保存"),
            )

    async def _mem0_delete(
        self,
        memory_id: str,
        *,
        user_id: str,
        chat_id: str,
        approval_token: str | None,
    ) -> str | QuickCommandResult:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", memory_id):
            return "用法：`/mem0 delete MEMORY_ID`（只接受单个记忆 ID）"
        if self.mem0 is None:
            return "🧠 Mem0：⚠️ 未配置 MEM0_BASE_URL"
        denied = self._memory_write_denial(
            user_id=user_id,
            approval_token=approval_token,
            operation="delete",
            tool_name="memory_delete",
        )
        if denied:
            return denied
        try:
            project_id = await self._current_mem0_project(user_id, chat_id)
            await self.mem0.delete(
                memory_id,
                actor_id=user_id,
                **self._mem0_project_kwargs(project_id),
            )
            text = (
                f"🧠 Mem0 记忆已删除：✅\n• ID：`{memory_id}`\n"
                f"• Scope：{self._mem0_scope_label(user_id, project_id)}"
            )
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0 删除"),
            )
        except Exception as exc:
            log.error("quick_mem0_delete_failed", error=type(exc).__name__)
            text = "🧠 Mem0 记忆删除：⚠️ 服务不可用，未阻塞其他任务"
            return QuickCommandResult(
                text,
                build_sections_card([text], title="Luck Agent · Mem0 删除"),
            )

    def _memory_write_denial(
        self,
        *,
        user_id: str,
        approval_token: str | None,
        operation: str,
        tool_name: str,
    ) -> str | None:
        if self.permission_policy is not None:
            if not self.permission_policy.allows_user(user_id):
                return "⛔ 当前用户无权修改 Mem0 记忆"
            if not self.permission_policy.allows_service("mem0"):
                return "⛔ 当前用户无权访问 Mem0 服务"
            if not self.permission_policy.allows_operation(operation):
                return f"⛔ 当前用户无权执行操作：`{operation}`"
        if not approval_token or self.approval_checker is None:
            return "⚠️ Mem0 记忆变更必须先完成一次性确认"
        try:
            approved = self.approval_checker(
                user_id,
                approval_token,
                tool_name,
                {"service": "mem0", "operation": operation},
            )
        except Exception:
            approved = False
        if not approved:
            return "⛔ Mem0 记忆变更确认码无效、过期或范围不匹配"
        return None


def _format_service_probe(service: str, result: Any) -> str:
    mark = "✅" if getattr(result, "ok", False) else "⚠️"
    target = getattr(result, "target", None)
    target_line = f"\n• 目标：`{target.display}`" if target is not None else ""
    output = str(getattr(result, "output", "") or "").strip()
    error = str(getattr(result, "error", "") or "").strip()
    if service == "a2a" and output:
        try:
            card = json.loads(output)
            name = str(card.get("name") or "unknown")
            version = str(card.get("version") or "unknown")
            return f"🛰️ A2A API：{mark}{target_line}\n• Agent：`{name}`\n• 版本：`{version}`"
        except (TypeError, ValueError):
            pass
    body = output or error or "无返回内容"
    if not getattr(result, "ok", False) and error and output:
        body = f"{error}\n{output}"
    return f"🧩 {service} API：{mark}{target_line}\n{body}"


def _prefix_result(prefix: str, result: str | QuickCommandResult) -> str | QuickCommandResult:
    if isinstance(result, QuickCommandResult):
        return QuickCommandResult(f"{prefix}\n{result.text}", result.card)
    return f"{prefix}\n{result}"


def _memory_text(item: dict[str, Any]) -> str:
    for key in ("memory", "text", "content"):
        value = item.get(key)
        if isinstance(value, str):
            return " ".join(value.split())
    return ""


def _memory_result_count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("memories", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        return 1 if payload else 0
    return 0
