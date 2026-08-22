from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    label: str
    backend: str
    description: str
    restartable: bool = False


@dataclass(frozen=True)
class ServiceOperationSpec:
    """Fixed contract for one mutating service operation."""

    service_id: str
    operation: str
    entrypoint: str
    rollback_strategy: str
    verification: str


SERVICE_CATALOG: tuple[ServiceSpec, ...] = (
    ServiceSpec("mem0", "Mem0", "mem0", "记忆 API 状态、smoke 和搜索"),
    ServiceSpec("a2a", "A2A", "probe", "目标机 A2A Agent Card 健康检查"),
    ServiceSpec("new-api", "new-api", "http", "OpenAI-compatible models 健康检查"),
    ServiceSpec("luck-agent", "Luck Agent", "sysops", "宿主机服务状态", restartable=True),
)


SERVICE_OPERATIONS: tuple[ServiceOperationSpec, ...] = (
    ServiceOperationSpec(
        service_id="luck-agent",
        operation="restart",
        entrypoint="sudo -n /usr/local/sbin/luck-agent-restart",
        rollback_strategy=(
            "restart 不修改持久化配置；若启动验收失败，由 systemd Restart=always 保持恢复，"
            "版本回滚必须走独立、显式批准的 Git rollback 操作"
        ),
        verification="systemctl is-active luck-agent.service + /health",
    ),
)


def get_service(service_id: str) -> ServiceSpec | None:
    normalized = str(service_id or "").strip().lower()
    return next((item for item in SERVICE_CATALOG if item.service_id == normalized), None)


def get_service_operation(service_id: str, operation: str) -> ServiceOperationSpec | None:
    normalized_service = str(service_id or "").strip().lower()
    normalized_operation = str(operation or "").strip().lower()
    return next(
        (
            item
            for item in SERVICE_OPERATIONS
            if item.service_id == normalized_service and item.operation == normalized_operation
        ),
        None,
    )


def format_service_catalog(*, allowed: set[str] | frozenset[str] | None = None) -> str:
    specs = [
        item
        for item in SERVICE_CATALOG
        if allowed is None or item.service_id in allowed
    ]
    if not specs:
        return "🧩 当前没有已授权的服务"
    lines = ["🧩 可用服务："]
    for item in specs:
        lines.append(f"• `{item.service_id}`：{item.description}")
    lines.append("用法：`/vps service SERVICE status|smoke|search 关键词`")
    return "\n".join(lines)
