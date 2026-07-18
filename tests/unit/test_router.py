from __future__ import annotations

from pathlib import Path

from core.output_parser import IntentType
from core.router import ToolRouter
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


class NamedTool(Tool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.args_schema = {}

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult.ok(tool_name=self.name)


def _registry() -> ToolRegistry:
    return ToolRegistry(NamedTool(name) for name in ["a", "b", "c", "d", "e", "fallback"])


def _write_rules(path: Path, tool: str = "a") -> None:
    path.write_text(
        f'''
rules:
  - name: "match"
    patterns: ["search"]
    tools: ["{tool}", "b", "c", "d", "e"]
fallback_tools:
  - "fallback"
  - "a"
  - "b"
'''.strip(),
        encoding="utf-8",
    )


def test_rule_match_returns_three_to_five_tool_subset(tmp_path: Path) -> None:
    rules = tmp_path / "routing_rules.yaml"
    _write_rules(rules)

    tools = ToolRouter(_registry(), rules_path=rules).route("please search", IntentType.ACTION)

    assert 3 <= len(tools) <= 5
    assert [tool.name for tool in tools] == ["a", "b", "c", "d", "e"]


def test_route_failure_returns_fallback_tool_set(tmp_path: Path) -> None:
    rules = tmp_path / "routing_rules.yaml"
    _write_rules(rules)

    tools = ToolRouter(_registry(), rules_path=rules).route("unknown", IntentType.ACTION)

    assert [tool.name for tool in tools] == ["fallback", "a", "b"]


def test_reload_rules_hot_update_applies_immediately(tmp_path: Path) -> None:
    rules = tmp_path / "routing_rules.yaml"
    _write_rules(rules, "a")
    router = ToolRouter(_registry(), rules_path=rules)
    _write_rules(rules, "fallback")

    router.reload_rules()

    assert [tool.name for tool in router.route("search", IntentType.ACTION)][0] == "fallback"
