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

import structlog

log = structlog.get_logger()

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

    async def run_script(self, script: str, suffix: str = ".sh") -> dict:
        """将多行脚本写入临时文件后执行。"""
        tmp = self.work_dir / f"_script_{int(time.time())}{suffix}"
        tmp.write_text(script)
        tmp.chmod(0o755)
        result = await self.run(f"bash {tmp}")
        tmp.unlink(missing_ok=True)
        return result


class FileManager:
    """VPS 文件管理（读写删移动列表）。"""

    def __init__(self, base_dir: str) -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, path: str) -> Path:
        """确保路径在 base_dir 内（防止目录遍历）。"""
        resolved = (self.base / path).resolve()
        if not str(resolved).startswith(str(self.base.resolve())):
            raise PermissionError(f"路径越界：{path}")
        return resolved

    def list_dir(self, path: str = "") -> list[dict]:
        target = self._safe_path(path)
        if not target.exists():
            return []
        items = []
        for p in sorted(target.iterdir()):
            items.append({
                "name":    p.name,
                "type":    "dir" if p.is_dir() else "file",
                "size":    p.stat().st_size if p.is_file() else 0,
                "modified": p.stat().st_mtime,
            })
        return items

    def read_file(self, path: str, max_chars: int = 8000) -> str:
        p = self._safe_path(path)
        content = p.read_text(errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n…（文件共 {len(content)} 字符，已截断）"
        return content

    def write_file(self, path: str, content: str) -> dict:
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"path": str(p), "size": p.stat().st_size}

    def delete(self, path: str) -> dict:
        p = self._safe_path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"deleted": str(p)}

    def move(self, src: str, dst: str) -> dict:
        s = self._safe_path(src)
        d = self._safe_path(dst)
        shutil.move(str(s), str(d))
        return {"moved": str(d)}

    def disk_usage(self) -> dict:
        total, used, free = shutil.disk_usage(self.base)
        return {
            "total_gb": round(total / 1e9, 2),
            "used_gb":  round(used  / 1e9, 2),
            "free_gb":  round(free  / 1e9, 2),
        }


# ── Tool Schemas ────────────────────────────────────────────────────────────
SHELL_TOOL_SCHEMAS = [
    {
        "name": "run_shell",
        "description": "在 VPS 上执行任意 shell 命令。当需要查看文件、运行脚本、操作 git、安装依赖、检查进程、或完成任何系统级操作时，优先调用此工具探索，而不是回答说不会。不确定命令是否正确时，先执行 dry-run 或查看命令，再执行实际操作。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "cwd":     {"type": "string", "description": "工作目录（可选）"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_script",
        "description": "执行多行 bash 脚本。",
        "parameters": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "多行 bash 脚本内容"},
            },
            "required": ["script"],
        },
    },
    {
        "name": "list_files",
        "description": "列出 VPS 工作目录中的文件和文件夹。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对于工作目录的路径"},
            },
        },
    },
    {
        "name": "read_file",
        "description": "读取 VPS 上的文件内容。",
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
        "description": "在 VPS 上写入文件（覆盖）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "disk_usage",
        "description": "查看 VPS 磁盘使用情况。",
        "parameters": {"type": "object", "properties": {}},
    },
]
