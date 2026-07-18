from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolResult


ALLOWED_PREFIXES = {
    "ls",
    "cat",
    "grep",
    "find",
    "echo",
    "pwd",
    "python3",
    "pip",
    "curl",
    "wget",
    "git",
    "df",
    "ps",
    "env",
}

DANGEROUS_PATTERNS = (
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r">\s*/etc/"),
    re.compile(r"\|\s*sh\b"),
    re.compile(r"\|\s*bash\b"),
)


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a whitelisted shell command in AGENT_WORKDIR. Parameters: command "
        "(string), timeout (seconds, default 15). Returns combined stdout/stderr."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number", "default": 15},
        },
        "required": ["command"],
    }

    async def execute(self, command: str, timeout: float | None = None) -> ToolResult:
        return await self.run(command, timeout)

    async def run(self, command: str = "", timeout: float | None = None, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        command = str(command or kwargs.get("command", "")).strip()
        timeout = float(
            timeout
            if timeout is not None
            else kwargs.get("timeout", os.environ.get("SHELL_TIMEOUT_SECONDS", "15"))
        )
        error = self.validate_command(command)
        if error:
            return ToolResult.fail(error=error, tool_name=self.name)
        workdir = Path(os.environ.get("AGENT_WORKDIR", "/home/agent/workspace"))
        workdir.mkdir(parents=True, exist_ok=True)
        timeout = max(0.1, min(timeout, 15))
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                return ToolResult.fail(error="TIMEOUT_ERROR", tool_name=self.name)
            stdout = await proc.stdout.read() if proc.stdout else b""
            stderr = await proc.stderr.read() if proc.stderr else b""
        except Exception as exc:
            return ToolResult.fail(error=str(exc), tool_name=self.name)
        output = self._truncate((stdout + stderr).decode("utf-8", errors="replace"))
        status = "ok" if getattr(proc, "returncode", 1) == 0 else "error"
        if status == "error":
            return ToolResult.fail(
                error=f"command exited with {getattr(proc, 'returncode', 1)}",
                data={"output": output, "returncode": getattr(proc, "returncode", 1)},
                tool_name=self.name,
            )
        return ToolResult.ok(
            data={"output": output, "returncode": getattr(proc, "returncode", 0)},
            tool_name=self.name,
        )

    def validate_command(self, command: str) -> str:
        if not command:
            return "command is required"
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(command):
                return "dangerous command pattern rejected"
        try:
            parts = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            return f"invalid command: {exc}"
        if not parts:
            return "command is required"
        if parts[0] not in ALLOWED_PREFIXES:
            return f"command prefix not allowed: {parts[0]}"
        return ""

    def _truncate(self, output: str) -> str:
        max_chars = int(os.environ.get("SHELL_MAX_OUTPUT_CHARS", "4000"))
        if len(output) <= max_chars:
            return output
        return output[:max_chars] + "\n[truncated]"

    def docstring(self, task_context: str = "", experience: str = "") -> str:
        base = super().docstring(task_context=task_context, experience=experience)
        return (
            base
            + "\nL1: Parameters: command, timeout. Return format: {output, returncode}."
            + "\nL2: 当任务需要本地操作、文件处理、运行脚本、查看目录或 shell 命令时使用。"
        )
