from __future__ import annotations

import re
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


def operation_requires_approval(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name in _MUTATING_TOOL_NAMES:
        return True
    if tool_name != "shell":
        return False
    command = str(args.get("command") or args.get("cmd") or "")
    return bool(_MUTATING_SHELL_RE.search(command))


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
