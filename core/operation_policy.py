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


@dataclass(frozen=True)
class OperationPermissionPolicy:
    """Optional target/service/operation allowlist for tool execution."""

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
            if not target or target not in self.allowed_targets:
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
