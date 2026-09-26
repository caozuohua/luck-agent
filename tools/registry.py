from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterable

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from tools.base import Tool


class ToolRegistrationError(ValueError):
    pass


class ToolNotFoundError(KeyError):
    pass


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise ToolRegistrationError("registered object must inherit tools.base.Tool")
        if not tool.name or not tool.name.strip():
            raise ToolRegistrationError("tool name is required")
        if tool.name in self._tools:
            raise ToolRegistrationError(f"duplicate tool: {tool.name}")
        if not isinstance(tool.args_schema, dict):
            raise ToolRegistrationError("tool args_schema must be an object")
        try:
            validator_for(tool.args_schema).check_schema(tool.args_schema)
        except SchemaError as exc:
            raise ToolRegistrationError(f"invalid argument schema for tool: {tool.name}") from exc
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def capability_snapshot(self) -> list[dict]:
        """Live local registry/config snapshot, never a remote health claim."""
        snapshot = []
        for tool in self.list():
            availability = tool.checked_availability()
            snapshot.append({
                "name": tool.name, "schema_version": tool.schema_version,
                "effect": tool.effect, "locally_ready": availability.ready,
                "reason": availability.reason,
            })
        return snapshot

    def register_builtin_tools(self) -> None:
        from tools.shell import ShellTool
        from tools.web_search import WebSearchTool

        for tool in (WebSearchTool(), ShellTool()):
            if tool.name not in self._tools:
                self.register(tool)

    def discover(self, package_name: str = "tools") -> None:
        """Import modules in a package and instantiate concrete Tool subclasses."""
        package = importlib.import_module(package_name)
        for module_info in pkgutil.iter_modules(package.__path__):
            if module_info.name in {"base", "registry"}:
                continue
            module = importlib.import_module(f"{package_name}.{module_info.name}")
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if obj is Tool or not issubclass(obj, Tool) or inspect.isabstract(obj):
                    continue
                if obj.__module__ != module.__name__:
                    continue
                self.register(obj())
