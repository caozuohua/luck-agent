"""
tools/shell_tools.py — Shell 执行 + 文件 I/O
大模型不可用时仍然可以执行 shell 命令和文件操作（通过 /cmd 前缀触发）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

from core.log import get_logger

log = get_logger()

# 危险命令黑名单（需要二次确认）
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "mkfs",
    "dd if=",
    "> /dev/",
    "shutdown",
    "reboot",
    ":(){ :|:& };:",  # fork bomb
]


class ShellExecutor:
    """安全的 Shell 命令执行器。"""

    def __init__(self, work_dir: str, timeout: int = 60, max_output: int = 4000) -> None:
        self.work_dir   = Path(work_dir)
        self.timeout    = timeout
        self.max_output = max_output
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def is_dangerous(self, cmd: str) -> bool:
        cmd_lower = cmd.lower()
        return any(p.lower() in cmd_lower for p in DANGEROUS_PATTERNS)

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        env_extra: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        """
        异步执行 shell 命令，返回：
        {"stdout": str, "stderr": str, "returncode": int, "elapsed": float, "truncated": bool}
        """
        run_dir = Path(cwd) if cwd else self.work_dir
        t0 = time.monotonic()

        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(run_dir),
                env=env,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout or self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "stdout":     "",
                    "stderr":     f"命令超时（{timeout or self.timeout}s）已终止",
                    "returncode": -1,
                    "elapsed":    round(time.monotonic() - t0, 2),
                    "truncated":  False,
                }

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            elapsed = round(time.monotonic() - t0, 2)
            truncated = False

            combined = stdout + ("\n[STDERR]\n" + stderr if stderr else "")
            if len(combined) > self.max_output:
                combined = combined[: self.max_output] + "\n…（输出已截断）"
                truncated = True

            log.info("shell_run", cmd=command[:80], rc=proc.returncode, elapsed=elapsed)
            return {
                "stdout":     combined,
                "stderr":     stderr if len(stderr) <= 500 else stderr[:500] + "…",
                "returncode": proc.returncode,
                "elapsed":    elapsed,
                "truncated":  truncated,
            }

        except Exception as e:
            return {
                "stdout":     "",
                "stderr":     str(e),
                "returncode": -1,
                "elapsed":    round(time.monotonic() - t0, 2),
                "truncated":  False,
            }


class FileManager:
    """VPS 文件管理。所有路径相对于 base_dir，返回给模型的路径也用相对路径。"""

    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, path: str) -> Path:
        """确保路径在 base_dir 内，不存在时创建父目录。"""
        resolved = (self.base / path).resolve()
        if not str(resolved).startswith(str(self.base.resolve())):
            raise PermissionError(f"路径越界：{path}")
        return resolved

    def _rel(self, p: Path) -> str:
        """返回相对于 base_dir 的路径，给模型看更友好。"""
        try:
            return str(p.relative_to(self.base))
        except ValueError:
            return str(p)

    def list_dir(self, path: str = ".") -> list[dict]:
        target = self._safe_path(path)
        if not target.exists():
            return []
        items = []
        for p in sorted(target.iterdir()):
            stat = p.stat()
            items.append({
                "name":     p.name,
                "type":     "dir" if p.is_dir() else "file",
                "size":     stat.st_size if p.is_file() else 0,
                "modified": int(stat.st_mtime),
            })
        return items

    def read_file(self, path: str, max_chars: int = 8000) -> dict:
        p = self._safe_path(path)
        if not p.exists():
            return {"error": f"文件不存在：{path}", "path": path}
        content = p.read_text(errors="replace")
        truncated = len(content) > max_chars
        return {
            "path":      path,
            "content":   content[:max_chars] + ("\n…（已截断）" if truncated else ""),
            "size":      len(content),
            "truncated": truncated,
        }

    def write_file(self, path: str, content: str) -> dict:
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"path": path, "size": p.stat().st_size}

    def delete(self, path: str) -> dict:
        p = self._safe_path(path)
        if not p.exists():
            return {"error": f"不存在：{path}"}
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"deleted": path}


# ── Tool Schemas ────────────────────────────────────────────────────────────
SHELL_TOOL_SCHEMAS = [
    {
        "name": "run_shell",
        "description": "在 VPS 上执行 shell 命令。需要运行命令、操作 git、安装软件、查看进程、执行脚本时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "shell 命令，多行脚本可直接换行"},
                "cwd":     {"type": "string", "description": "工作目录（可选，默认项目目录）"},
                "timeout": {"type": "integer", "description": "超时秒数（可选，默认 60）"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "list_files",
        "description": "列出 VPS 某目录下的文件和文件夹。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径（相对于工作目录，默认根目录）"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "读取 VPS 上某个文件的内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "在 VPS 上写入文件，自动创建父目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": "删除 VPS 上的文件或空目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件或目录路径"},
            },
            "required": ["path"],
        },
    },
]
