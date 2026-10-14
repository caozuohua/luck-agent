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
class ServiceAssetSpec:
    """Read-only inventory metadata for one service on one VPS."""

    target_id: str
    service_id: str
    label: str
    runtime: str
    scope: str = ""
    units: tuple[str, ...] = ()
    compose_project: str = ""
    ports: tuple[str, ...] = ()
    health_checks: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    logs: str = ""
    backups: str = ""
    change_boundary: str = "只读目录；暂无受控变更入口"
    observed_state: str = "已发现"
    last_verified: str = "2026-08-24"
    managed_service_id: str = ""

    @property
    def access_service_id(self) -> str:
        """Use the existing permission identity without creating an action."""
        return self.managed_service_id or self.service_id


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
    target_ids: tuple[str, ...] = ()

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
        if any(not target.strip() for target in self.target_ids):
            raise ValueError("invalid target id in service operation contract")

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

    def supports_target(self, target_id: str = "", provider: str = "") -> bool:
        """Check both provider and, when declared, the exact target identity."""
        return self.supports_provider(provider) and (
            not self.target_ids
            or str(target_id or "").strip().lower() in self.target_ids
        )


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


# Inventory only.  Adding an asset here must never add a restart/backup
# operation; those remain exclusively defined in SERVICE_OPERATIONS below.
SERVICE_ASSET_CATALOG: tuple[ServiceAssetSpec, ...] = (
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "hermes-gateway", "Hermes Gateway", "systemd",
        units=("hermes-gateway.service",), health_checks=("systemd active",),
        dependencies=("a2a-mcp-companion（由本服务拉起）",),
        logs="journald: hermes-gateway.service", backups="未登记服务级备份对象",
        change_boundary="受控：restart（现有契约）；暂无新增 backup/upgrade",
        observed_state="active", managed_service_id="hermes-gateway",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "a2a", "Hermes A2A Bridge", "systemd",
        units=("hermes-a2a-bridge.service",), ports=("Tailscale :8765",),
        health_checks=("/healthz", "Agent Card probe"),
        logs="journald: hermes-a2a-bridge.service", backups="未登记服务级备份对象",
        change_boundary="受控：restart（现有契约）；暂无新增 backup/upgrade",
        observed_state="active / healthz 200", managed_service_id="a2a",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "a2a-mcp-companion", "A2A MCP companion", "子进程",
        scope="随 hermes-gateway.service", dependencies=("hermes-gateway.service",),
        logs="journald: hermes-gateway.service", backups="未登记独立备份对象",
        change_boundary="只读目录；随父服务受控 restart，不提供独立操作",
        observed_state="随父服务运行", managed_service_id="hermes-gateway",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "new-api", "new-api",
        "Docker container（固定 systemd 入口）", units=("new-api.service",),
        ports=("127.0.0.1:3000",), health_checks=("/v1/models（需认证）",),
        logs="journald: new-api.service；Docker logs（由固定入口管理）",
        backups="scoped new-api backup（现有 GCP 契约）",
        change_boundary="受控：restart + backup（现有契约）；暂无 restore/upgrade",
        observed_state="active / 未认证请求 401", managed_service_id="new-api",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "x-ui", "x-ui", "systemd",
        units=("x-ui.service",), ports=(":50404（管理面）",),
        health_checks=("进程/监听状态；无独立公开健康契约",),
        logs="journald: x-ui.service", backups="未登记服务级备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="active",
        managed_service_id="x-ui",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "xray", "Xray", "systemd",
        units=("xray.service",), ports=("127.0.0.1:44301",),
        health_checks=("进程/监听状态；经 Nginx 分流",), dependencies=("nginx.service",),
        logs="journald: xray.service", backups="未登记服务级备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="active",
        managed_service_id="xray",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "nginx", "Nginx", "systemd",
        units=("nginx.service",), ports=(":80", ":443"),
        health_checks=("监听状态；反向代理边界",),
        dependencies=("x-ui / Xray / Hermes 路由目标",),
        logs="journald: nginx.service；系统访问/错误日志",
        backups="未登记服务级备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="active",
        managed_service_id="nginx",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "docker", "Docker Engine", "systemd",
        units=("docker.service",), health_checks=("systemd active",),
        logs="journald: docker.service", backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="active",
        managed_service_id="docker",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "tailscale", "Tailscale", "systemd",
        units=("tailscaled.service",), ports=("Tailscale overlay",),
        health_checks=("systemd/节点连通性",), logs="journald: tailscaled.service",
        backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="运行中",
        managed_service_id="tailscale",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "fail2ban", "fail2ban / 系统安全组件", "systemd",
        units=("fail2ban.service",), health_checks=("systemd active",),
        logs="journald: fail2ban.service", backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="运行中",
        managed_service_id="fail2ban",
    ),
    ServiceAssetSpec(
        "gcp-free-vps-oregon", "vps-sysops-monitor", "vps_sysops 监控", "systemd timer",
        health_checks=("timer 上次/下次运行状态",), logs="journald: monitor timer/job",
        backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="已配置",
        managed_service_id="vps-sysops-monitor",
    ),
    ServiceAssetSpec(
        "az-free-vm", "hermes-gateway", "Hermes Gateway", "user systemd",
        scope="用户级", units=("hermes-gateway.service",),
        health_checks=("systemctl --user active",),
        dependencies=("a2a-mcp-companion（由本服务拉起）",),
        logs="用户级 journald: hermes-gateway.service", backups="未登记服务级备份对象",
        change_boundary="受控：restart（现有 Azure 契约）；暂无新增 backup/upgrade",
        observed_state="active", managed_service_id="hermes-gateway",
    ),
    ServiceAssetSpec(
        "az-free-vm", "a2a", "Hermes A2A Bridge", "user systemd",
        scope="用户级", units=("hermes-a2a-bridge.service",),
        ports=("Tailscale :8765（无公网入站）",),
        health_checks=("/healthz", "Agent Card probe"),
        logs="用户级 journald: hermes-a2a-bridge.service", backups="未登记服务级备份对象",
        change_boundary="受控：restart（现有契约）；暂无新增 backup/upgrade",
        observed_state="active / healthz 200", managed_service_id="a2a",
    ),
    ServiceAssetSpec(
        "az-free-vm", "tailscale", "Tailscale", "systemd",
        units=("tailscaled.service",), ports=("Tailscale overlay",),
        health_checks=("systemd/节点连通性",), logs="journald: tailscaled.service",
        backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="运行中",
        managed_service_id="tailscale",
    ),
    ServiceAssetSpec(
        "az-free-vm", "fail2ban", "fail2ban / 系统安全组件", "systemd",
        units=("fail2ban.service",), health_checks=("systemd active",),
        logs="journald: fail2ban.service", backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="运行中",
        managed_service_id="fail2ban",
    ),
    ServiceAssetSpec(
        "az-free-vm", "vps-sysops-monitor", "vps_sysops 监控", "systemd timer",
        health_checks=("timer 上次/下次运行状态",), logs="journald: monitor timer/job",
        backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="已配置",
        managed_service_id="vps-sysops-monitor",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "luck-agent", "Luck Agent", "systemd",
        units=("luck-agent.service",), ports=("Lark WebSocket；health port",),
        health_checks=("/health",), logs="journald: luck-agent.service",
        backups="未登记服务级备份对象",
        change_boundary="受控：restart（现有契约）；暂无新增 backup/upgrade",
        observed_state="active / /health 200", managed_service_id="luck-agent",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "mem0-api", "Mem0 API", "Docker Compose",
        compose_project="/opt/mem0/server", ports=("Tailscale :8888",),
        health_checks=("/docs + 认证 smoke（不以 /health 判断）",),
        dependencies=("mem0-postgres",), logs="Docker Compose logs: API container",
        backups="随 Mem0 PostgreSQL 每日备份 timer 观察；无 Agent backup 入口",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="container running",
        managed_service_id="mem0",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "mem0-dashboard", "Mem0 Dashboard", "Docker Compose",
        compose_project="/opt/mem0/server", ports=("Tailscale :3000",),
        health_checks=("container healthy",), dependencies=("mem0-api",),
        logs="Docker Compose logs: dashboard container",
        backups="不单独备份；依赖 Mem0 数据库备份",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="healthy",
        managed_service_id="mem0",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "mem0-postgres", "Mem0 PostgreSQL/pgvector", "Docker Compose",
        compose_project="/opt/mem0/server", health_checks=("container healthy",),
        logs="Docker Compose logs: postgres container",
        backups="每日备份 timer（现有主机侧对象）",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="healthy",
        managed_service_id="mem0",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "mem0-postgres-backup", "Mem0 PostgreSQL 每日备份", "systemd timer",
        health_checks=("timer 上次/下次运行状态",), dependencies=("mem0-postgres",),
        logs="journald: backup timer/job", backups="Mem0 PostgreSQL（只读展示备份关系）",
        change_boundary="只读目录；不新增 backup/restore 操作", observed_state="已配置",
        managed_service_id="mem0",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "docker", "Docker Engine", "systemd",
        units=("docker.service",), health_checks=("systemd active",),
        logs="journald: docker.service", backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="active",
        managed_service_id="docker",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "tailscale", "Tailscale", "systemd",
        units=("tailscaled.service",), ports=("Tailscale overlay",),
        health_checks=("systemd/节点连通性",), logs="journald: tailscaled.service",
        backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="运行中",
        managed_service_id="tailscale",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "fail2ban", "fail2ban / 系统安全组件", "systemd",
        units=("fail2ban.service",), health_checks=("systemd active",),
        logs="journald: fail2ban.service", backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="运行中",
        managed_service_id="fail2ban",
    ),
    ServiceAssetSpec(
        "aws-codex-vps", "vps-sysops-monitor", "vps_sysops 监控", "systemd timer",
        health_checks=("timer 上次/下次运行状态",), logs="journald: monitor timer/job",
        backups="不作为业务备份对象",
        change_boundary="只读目录；暂无受控 restart/backup", observed_state="已配置",
        managed_service_id="vps-sysops-monitor",
    ),
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
        service_id="new-api",
        operation="backup",
        entrypoint="sudo -n bash /opt/vps_sysops/scripts/new_api_backup.sh",
        rollback_strategy=(
            "备份只新增权限为 0600 的时间戳归档，不修改 new-api 配置、数据库或服务状态；"
            "失败的临时归档会由脚本清理，恢复必须走独立、显式批准的 restore 操作"
        ),
        verification=(
            "脚本内 SHA-256 校验 + tar 可读性 + SQLite integrity_check；"
            "仅允许 GCP 的 gcp-free-vps-oregon 目标"
        ),
        preconditions=(
            "selected target is exactly gcp-free-vps-oregon",
            "scoped new-api backup script is installed on the selected target",
            "operator allowlist and one-time approval passed",
        ),
        idempotency="safe to retry; creates a timestamped archive and does not restart new-api",
        target_providers=("gcp",),
        target_ids=("gcp-free-vps-oregon",),
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


def get_service_operations(service_id: str) -> tuple[ServiceOperationSpec, ...]:
    normalized = str(service_id or "").strip().lower()
    return tuple(item for item in SERVICE_OPERATIONS if item.service_id == normalized)


def service_action_names(spec: ServiceSpec) -> tuple[str, ...]:
    """Return the read-only and contracted mutating actions visible to operators."""
    if spec.backend == "mem0":
        actions = ["status", "list", "smoke", "search"]
    elif spec.backend == "sysops":
        actions = ["status", "health", "list"]
    else:
        actions = ["status", "health"]
    actions.extend(item.operation for item in get_service_operations(spec.service_id))
    return tuple(dict.fromkeys(actions))


def service_operation_constraints(spec: ServiceSpec) -> tuple[str, ...]:
    """Describe target constraints without exposing shell entrypoints."""
    constraints: list[str] = []
    for operation in get_service_operations(spec.service_id):
        if operation.target_ids:
            constraints.append(
                f"{operation.operation}: target=" + ",".join(operation.target_ids)
            )
        elif operation.target_providers:
            constraints.append(
                f"{operation.operation}: provider=" + ",".join(operation.target_providers)
            )
    return tuple(constraints)


def get_service_assets(
    *,
    allowed: set[str] | frozenset[str] | None = None,
) -> tuple[ServiceAssetSpec, ...]:
    """Return inventory rows filtered by the existing service allowlist."""
    if allowed is None:
        return SERVICE_ASSET_CATALOG
    return tuple(
        asset for asset in SERVICE_ASSET_CATALOG
        if asset.access_service_id in allowed or asset.service_id in allowed
    )


def _asset_lines(asset: ServiceAssetSpec) -> list[str]:
    def joined(values: tuple[str, ...], fallback: str = "未登记") -> str:
        return "、".join(values) if values else fallback

    lines = [
        f"• `{asset.service_id}` · {asset.label}｜{asset.runtime}{(' / ' + asset.scope) if asset.scope else ''}",
        f"  状态基线：`{asset.observed_state}`（核验 {asset.last_verified}）",
    ]
    if asset.units:
        lines.append(f"  unit：`{joined(asset.units)}`")
    if asset.compose_project:
        lines.append(f"  Compose：`{asset.compose_project}`")
    lines.append(f"  端口/边界：{joined(asset.ports)}")
    lines.append(f"  健康：{joined(asset.health_checks)}")
    lines.append(f"  依赖：{joined(asset.dependencies)}")
    lines.append(f"  日志：{asset.logs or '未登记'}")
    lines.append(f"  备份：{asset.backups or '未登记'}")
    lines.append(f"  变更边界：{asset.change_boundary}")
    return lines


def format_service_asset_catalog(
    *,
    allowed: set[str] | frozenset[str] | None = None,
) -> str:
    """Format the three-VPS read-only service asset inventory."""
    assets = get_service_assets(allowed=allowed)
    if not assets:
        return "🧩 当前没有已授权的服务资产"
    lines = [
        "🧩 三台 VPS 只读服务资产目录：",
        "以下是 2026-08-24 运行态核验基线；实时状态请用 `/vps services`。",
        "目录发现不会新增 restart/backup 权限；变更边界仅引用现有固定契约。",
    ]
    current_target = ""
    for asset in assets:
        if asset.target_id != current_target:
            current_target = asset.target_id
            lines.append("")
            lines.append(f"### `{current_target}`")
        lines.extend(_asset_lines(asset))
    return "\n".join(lines)


def format_service_catalog(*, allowed: set[str] | frozenset[str] | None = None) -> str:
    specs = [
        item
        for item in SERVICE_CATALOG
        if allowed is None or item.service_id in allowed
    ]
    if not specs:
        return "🧩 当前没有已授权的服务"
    lines = ["🧩 已登记控制服务："]
    for item in specs:
        lines.append(f"• `{item.service_id}`：{item.description}")
        lines.append(f"  操作：`{'|'.join(service_action_names(item))}`")
        for constraint in service_operation_constraints(item):
            lines.append(f"  限制：`{constraint}`")
    lines.append("用法：`/vps service SERVICE ACTION [关键词]`")
    lines.append("")
    lines.append(format_service_asset_catalog(allowed=allowed))
    return "\n".join(lines)
