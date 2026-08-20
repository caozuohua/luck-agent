from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    label: str
    backend: str
    description: str


SERVICE_CATALOG: tuple[ServiceSpec, ...] = (
    ServiceSpec("mem0", "Mem0", "mem0", "记忆 API 状态、smoke 和搜索"),
    ServiceSpec("a2a", "A2A", "sysops", "宿主机服务状态"),
    ServiceSpec("new-api", "new-api", "sysops", "宿主机服务状态"),
    ServiceSpec("luck-agent", "Luck Agent", "sysops", "宿主机服务状态"),
)


def get_service(service_id: str) -> ServiceSpec | None:
    normalized = str(service_id or "").strip().lower()
    return next((item for item in SERVICE_CATALOG if item.service_id == normalized), None)


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
