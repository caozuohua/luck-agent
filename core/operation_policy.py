from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_MUTATING_SHELL_RE = re.compile(
    r"(?:"
    r"\bgit\s+(?:commit|push|reset|clean|checkout)\b"
    r"|\b(?:curl|wget)\b.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--request\s+(?:POST|PUT|PATCH|DELETE))"
    r"|\b(?:pip|pip3)\s+(?:install|uninstall)\b"
    r"|\bpython(?:3)?\b"
    r"|\b(?:powershell|pwsh|cmd)\b"
    r"|\bsystemctl\s+(?:start|stop|restart|enable|disable|mask|unmask)\b"
    r"|\b(?:rm|mv|cp|mkdir|touch)\b"
    r")",
    re.IGNORECASE,
)

_MUTATING_TOOL_NAMES = frozenset(
    {
        "vps_sysops_write",
        "service_restart",
        "service_update",
        "deploy",
        "delete",
        "restore",
    }
)

_SYSTEMCTL_RE = re.compile(
    r"\bsystemctl\s+(?P<operation>start|stop|restart|enable|disable|mask|unmask)\s+(?P<service>[\w@.:-]+)",
    re.IGNORECASE,
)

_REQUEST_OPERATION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"重启|重启服务|\brestart\b|\breboot\b", "restart"),
    (r"停止|停止服务|\bstop\b", "stop"),
    (r"启动|启动服务|\bstart\b", "start"),
    (r"部署|\bdeploy\b", "deploy"),
    (r"升级|\bupgrade\b", "upgrade"),
    (r"回滚|\brollback\b", "rollback"),
    (r"删除|清理|\bdelete\b|\bremove\b|\brm\b", "delete"),
    (r"安装|\binstall\b", "install"),
    (r"卸载|\buninstall\b", "uninstall"),
    (r"推送|\bpush\b", "push"),
    (r"写入|修改|\bwrite\b|\bupdate\b", "write"),
    (r"备份|\bbackup\b", "backup"),
    (r"恢复|\brestore\b", "restore"),
)


@dataclass(frozen=True)
class OperationPermissionPolicy:
    """Optional target/service/operation allowlist for tools and quick commands."""

    allowed_targets: frozenset[str] = frozenset()
    allowed_services: frozenset[str] = frozenset()
    allowed_operations: frozenset[str] = frozenset()

    @classmethod
    def from_csv(
        cls,
        *,
        targets: str = "",
        services: str = "",
        operations: str = "",
    ) -> "OperationPermissionPolicy":
        return cls(
            allowed_targets=_parse_csv(targets),
            allowed_services=_parse_csv(services),
            allowed_operations=_parse_csv(operations),
        )

    def allows(self, tool_name: str, args: dict[str, Any]) -> bool:
        if not operation_permission_applies(tool_name, args):
            return True
        if self.allowed_targets:
            target = _operation_target(args)
            if not self.allows_target(target):
                return False
        if self.allowed_services:
            service = _operation_service(tool_name, args)
            if not service or service not in self.allowed_services:
                return False
        if self.allowed_operations:
            operation = _operation_name(tool_name, args)
            if not operation or operation not in self.allowed_operations:
                return False
        return True

    def allows_target(self, target: str) -> bool:
        """Check a target ID for both read-only commands and tool execution."""
        normalized = str(target or "").strip().lower()
        return not self.allowed_targets or bool(normalized and normalized in self.allowed_targets)


@dataclass(frozen=True)
class OperationScope:
    operation: str = ""
    target: str = ""
    service: str = ""

    def matches(self, actual: "OperationScope") -> bool:
        """Match only fields explicitly named by the approved request."""
        return all(
            not expected or expected == observed
            for expected, observed in (
                (self.operation, actual.operation),
                (self.target, actual.target),
                (self.service, actual.service),
            )
        )


def scope_from_request(text: str) -> OperationScope:
    normalized = " ".join(text.strip().split())
    operation = ""
    for pattern, value in _REQUEST_OPERATION_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            operation = value
            break

    target = ""
    target_match = re.search(
        r"(?:目标|target|host|server|instance)\s*(?:为|是|=|:)?\s*([\w.@:-]+)",
        normalized,
        re.IGNORECASE,
    )
    if target_match:
        target = target_match.group(1).lower()

    service = ""
    service_match = re.search(
        r"([A-Za-z0-9_@.:-]+)\s*(?:服务|service)\b|(?:服务|service)\s*(?:为|是|=|:)?\s*([A-Za-z0-9_@.:-]+)",
        normalized,
        re.IGNORECASE,
    )
    if service_match:
        service = (service_match.group(1) or service_match.group(2)).lower()
    return OperationScope(operation=operation, target=target, service=service)


def scope_from_tool(tool_name: str, args: dict[str, Any]) -> OperationScope:
    return OperationScope(
        operation=_operation_name(tool_name, args),
        target=_operation_target(args),
        service=_operation_service(tool_name, args),
    )


def operation_requires_approval(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name in _MUTATING_TOOL_NAMES:
        return True
    if tool_name != "shell":
        return False
    command = str(args.get("command") or args.get("cmd") or "")
    return bool(_MUTATING_SHELL_RE.search(command))


def operation_permission_applies(tool_name: str, args: dict[str, Any]) -> bool:
    """Identify tools that belong to the target/service permission plane."""
    if tool_name == "shell" or tool_name in _MUTATING_TOOL_NAMES:
        return True
    if tool_name.startswith(("vps_", "service_")):
        return True
    return any(key in args for key in ("target", "host", "server", "instance", "service"))


def _parse_csv(value: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in value.split(",") if item.strip())


def _operation_target(args: dict[str, Any]) -> str:
    for key in ("target", "host", "server", "instance", "profile"):
        value = args.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    return ""


def _operation_service(tool_name: str, args: dict[str, Any]) -> str:
    for key in ("service", "service_name"):
        value = args.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    command = str(args.get("command") or args.get("cmd") or "")
    match = _SYSTEMCTL_RE.search(command)
    if match:
        return match.group("service").lower()
    return ""


def _operation_name(tool_name: str, args: dict[str, Any]) -> str:
    explicit = args.get("operation")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower()
    if tool_name.startswith("service_"):
        return tool_name.removeprefix("service_").lower()
    command = str(args.get("command") or args.get("cmd") or "")
    match = _SYSTEMCTL_RE.search(command)
    if match:
        return match.group("operation").lower()
    command_lower = command.lower()
    if re.search(r"\bgit\s+push\b", command_lower):
        return "push"
    if re.search(r"\bgit\s+commit\b", command_lower):
        return "commit"
    if re.search(r"\b(?:pip|pip3)\s+install\b", command_lower):
        return "install"
    if re.search(r"\b(?:curl|wget)\b.*(?:-x|--request)\s*(?:post|put|patch|delete)", command_lower):
        return "write"
    if re.search(r"\brm\b", command_lower):
        return "delete"
    return tool_name.lower()


def operation_description(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "shell":
        command = str(args.get("command") or args.get("cmd") or "shell operation").strip()
        executable = command.split(maxsplit=1)[0] if command else "shell"
        return f"shell:{executable[:80]}"
    details: list[str] = []
    for key in ("provider", "profile", "target", "service", "operation", "name"):
        value = args.get(key)
        if value is not None:
            details.append(f"{key}={str(value)[:80]}")
    suffix = " " + " ".join(details) if details else ""
    return f"{tool_name} operation{suffix}"
