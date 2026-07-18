from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.output_parser import IntentType
from tools.base import Tool
from tools.registry import ToolNotFoundError, ToolRegistry


@dataclass(frozen=True)
class RoutingRule:
    name: str
    patterns: tuple[str, ...]
    tools: tuple[str, ...]

    def matches(self, user_input: str) -> bool:
        lowered = user_input.lower()
        return any(pattern.lower() in lowered for pattern in self.patterns)


class ToolRouter:
    """Zero-LLM tool router backed by a small YAML rules file."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        rules_path: str | Path | None = None,
        fallback_tool_count: int = 5,
        watch_interval_seconds: float = 1.0,
    ) -> None:
        self.registry = registry
        self.rules_path = Path(rules_path) if rules_path else self._default_rules_path()
        self.fallback_tool_count = fallback_tool_count
        self.watch_interval_seconds = watch_interval_seconds
        self.rules: list[RoutingRule] = []
        self.fallback_tools: tuple[str, ...] = ()
        self._rules_mtime: float | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self.reload_rules()

    def route(self, user_input: str, intent: IntentType) -> list[Tool]:
        if intent is not IntentType.ACTION:
            return []
        for rule in self.rules:
            if rule.matches(user_input):
                selected = self._resolve_tools(rule.tools)
                if selected:
                    return selected[:5]
        return self._resolve_tools(self.fallback_tools)[: self.fallback_tool_count]

    def reload_rules(self) -> None:
        if not self.rules_path.exists():
            self.rules = []
            self.fallback_tools = tuple(self.registry.names()[: self.fallback_tool_count])
            return
        payload = self._parse_simple_yaml(self.rules_path.read_text(encoding="utf-8"))
        self.rules = [
            RoutingRule(
                name=str(rule.get("name", "")),
                patterns=tuple(str(item) for item in rule.get("patterns", [])),
                tools=tuple(str(item) for item in rule.get("tools", [])),
            )
            for rule in payload.get("rules", [])
        ]
        self.fallback_tools = tuple(str(item) for item in payload.get("fallback_tools", []))
        self._rules_mtime = self._get_rules_mtime()

    def start_watchdog(self) -> asyncio.Task[None]:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                self._watch_rules_loop(),
                name="routing-rules-watchdog",
            )
        return self._watchdog_task

    async def stop_watchdog(self) -> None:
        if self._watchdog_task is None:
            return
        self._watchdog_task.cancel()
        try:
            await self._watchdog_task
        except asyncio.CancelledError:
            pass
        self._watchdog_task = None

    async def _watch_rules_loop(self) -> None:
        while True:
            await asyncio.sleep(self.watch_interval_seconds)
            current_mtime = self._get_rules_mtime()
            if current_mtime != self._rules_mtime:
                self.reload_rules()

    def _get_rules_mtime(self) -> float | None:
        if not self.rules_path.exists():
            return None
        return self.rules_path.stat().st_mtime

    def _resolve_tools(self, names: tuple[str, ...]) -> list[Tool]:
        tools: list[Tool] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            try:
                tools.append(self.registry.get(name))
                seen.add(name)
            except ToolNotFoundError:
                continue
        return tools

    def _default_rules_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "config" / "routing_rules.yaml"

    def _parse_simple_yaml(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"rules": [], "fallback_tools": []}
        current_section = ""
        current_rule: dict[str, Any] | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "rules:":
                current_section = "rules"
                continue
            if line == "fallback_tools:":
                current_section = "fallback_tools"
                current_rule = None
                continue
            if current_section == "rules" and line.startswith("- name:"):
                current_rule = {"name": self._parse_scalar(line.split(":", 1)[1]), "patterns": [], "tools": []}
                payload["rules"].append(current_rule)
                continue
            if current_section == "rules" and current_rule is not None:
                if line.startswith("patterns:"):
                    current_rule["patterns"] = self._parse_inline_list(line.split(":", 1)[1])
                elif line.startswith("tools:"):
                    current_rule["tools"] = self._parse_inline_list(line.split(":", 1)[1])
                continue
            if current_section == "fallback_tools" and line.startswith("- "):
                payload["fallback_tools"].append(self._parse_scalar(line[2:]))
        return payload

    def _parse_inline_list(self, value: str) -> list[str]:
        value = value.strip()
        if not value:
            return []
        match = re.fullmatch(r"\[(.*)\]", value)
        if not match:
            return []
        inner = match.group(1).strip()
        if not inner:
            return []
        return [self._parse_scalar(item) for item in inner.split(",")]

    def _parse_scalar(self, value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            return value[1:-1]
        return value
