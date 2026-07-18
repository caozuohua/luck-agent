from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass
class ToolResult:
    status: Literal["ok", "error"]
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        *,
        data: Any = None,
        tool_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        meta = dict(metadata or {})
        if tool_name:
            meta.setdefault("tool_name", tool_name)
        return cls(status="ok", data=data, error=None, metadata=meta)

    @classmethod
    def fail(
        cls,
        *,
        error: str,
        data: Any = None,
        tool_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        meta = dict(metadata or {})
        if tool_name:
            meta.setdefault("tool_name", tool_name)
        return cls(status="error", data=data, error=error, metadata=meta)

    def with_timing(self, started_at: float) -> "ToolResult":
        self.metadata.setdefault("elapsed_ms", int((time.perf_counter() - started_at) * 1000))
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Tool(ABC):
    name: str = ""
    description: str = ""
    args_schema: dict[str, Any] = {}

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool and return a ToolResult."""

    def docstring(self, task_context: str = "", experience: str = "") -> str:
        lines = [
            f"Tool: {self.name}",
            f"Description: {self.description or 'No description provided.'}",
            f"Args schema: {self.args_schema or {}}",
        ]
        if task_context:
            lines.append(f"Task hint: Use this tool only if it helps with: {task_context}")
        if experience:
            lines.append(f"Experience: {experience}")
        return "\n".join(lines)
