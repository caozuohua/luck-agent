from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    label: str
    backend: str
    description: str
    restartable: bool = False
    target_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceOperationSpec:
    """Fixed contract for one mutating service operation."""

    service_id: str
    operation: str
    entrypoint: str
    rollback_strategy: str
    verification: str
    preconditions: tuple[str, ...] = ()
    idempotency: str = ""
    provider_entrypoints: tuple[tuple[str, str], ...] = ()
    target_providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = {
            "service_id": self.service_id,
            "operation": self.operation,
            "entrypoint": self.entrypoint,
            "rollback_strategy": self.rollback_strategy,
            "verification": self.verification,
            "idempotency": self.idempotency,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if not self.preconditions:
            missing.append("preconditions")
        if missing:
            raise ValueError(
                f"incomplete service operation contract: {', '.join(missing)}"
            )
        provider_ids = [provider.strip().lower() for provider, _ in self.provider_entrypoints]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("duplicate provider entrypoint in service operation contract")
        if any(not provider or not entrypoint.strip() for provider, entrypoint in self.provider_entrypoints):
            raise ValueError("invalid provider entrypoint in service operation contract")
        if any(not provider.strip() for provider in self.target_providers):
            raise ValueError("invalid target provider in service operation contract")

    def entrypoint_for(self, provider: str = "") -> str:
        normalized = str(provider or "").strip().lower()
        for provider_id, entrypoint in self.provider_entrypoints:
            if provider_id == normalized:
                return entrypoint
        return self.entrypoint

    def supports_provider(self, provider: str = "") -> bool:
        if not self.target_providers:
            return True
        return str(provider or "").strip().lower() in self.target_providers


SERVICE_CATALOG: tuple[ServiceSpec, ...] = (
    ServiceSpec("mem0", "Mem0", "mem0", "记忆 API 状态、浏览、搜索和 smoke"),
    ServiceSpec(
        "a2a",
        "A2A",
        "probe",
        "目标机 A2A Agent Card 健康检查",
        restartable=True,
        target_providers=("gcp", "azure"),
    ),
    ServiceSpec(
        "hermes-gateway",
        "Hermes Gateway",
        "probe",
        "Azure Hermes messaging gateway 健康检查",
        restartable=True,
        target_providers=("azure",),
    ),
    ServiceSpec(
        "new-api",
        "new-api",
        "http",
        "OpenAI-compatible models 健康检查",
        restartable=True,
    ),
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
        preconditions=("operator allowlist and one-time approval passed", "fixed restart wrapper is installed"),
        idempotency="safe to retry; restart does not modify persistent configuration",
    ),
    ServiceOperationSpec(
        service_id="new-api",
        operation="restart",
        entrypoint="sudo -n systemctl restart new-api.service && systemctl is-active new-api.service",
        rollback_strategy=(
            "restart 不修改 new-api 配置或数据；失败时可再次执行同一固定 restart，"
            "版本回滚必须走独立、显式批准的镜像或配置回滚操作"
        ),
        verification="systemctl is-active new-api.service + /v1/models",
        preconditions=("selected target is the configured new-api target", "operator allowlist and one-time approval passed"),
        idempotency="safe to retry; restart does not modify new-api configuration or data",
    ),
    ServiceOperationSpec(
        service_id="a2a",
        operation="restart",
        entrypoint=(
            "sudo -n systemctl restart hermes-a2a-bridge.service && "
            "systemctl is-active hermes-a2a-bridge.service"
        ),
        rollback_strategy=(
            "restart 不修改 A2A 配置或数据；失败时可再次执行同一固定 restart，"
            "版本回滚必须走 Hermes/A2A 独立、显式批准的部署回滚操作"
        ),
        verification="systemctl is-active hermes-a2a-bridge.service + Agent Card probe",
        provider_entrypoints=(
            (
                "azure",
                "systemctl --user restart hermes-a2a-bridge.service && "
                "systemctl --user is-active hermes-a2a-bridge.service",
            ),
        ),
        target_providers=("gcp", "azure"),
        preconditions=("selected target provider is GCP or Azure", "operator allowlist and one-time approval passed"),
        idempotency="safe to retry; restart does not modify A2A configuration or data",
    ),
    ServiceOperationSpec(
        service_id="hermes-gateway",
        operation="restart",
        entrypoint=(
            "systemctl --user restart hermes-gateway.service && "
            "systemctl --user is-active hermes-gateway.service"
        ),
        rollback_strategy=(
            "restart 不修改 Hermes 配置或数据；失败时可再次执行同一固定 user-service restart，"
            "版本回滚必须走 Hermes 独立、显式批准的部署回滚操作"
        ),
        verification="systemctl --user is-active hermes-gateway.service",
        target_providers=("azure",),
        preconditions=("selected target is Azure", "operator allowlist and one-time approval passed"),
        idempotency="safe to retry; restart does not modify Hermes configuration or data",
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
    lines.append("用法：`/vps service SERVICE status|list|smoke|search 关键词`")
    return "\n".join(lines)
