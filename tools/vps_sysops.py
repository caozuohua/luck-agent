from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path


_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


@dataclass(frozen=True)
class VpsSysopsResult:
    operation: str
    ok: bool
    output: str = ""
    error: str = ""
    returncode: int | None = None


class VpsSysopsAdapter:
    """Run a small allowlist of read-only vps_sysops scripts.

    The adapter deliberately does not accept a shell command from chat.  This
    keeps the existing vps_sysops scripts as the management boundary while
    preventing the Lark command path from becoming arbitrary remote shell.
    """

    OPERATIONS: dict[str, str] = {
        "status": "scripts/05_overview.sh",
        "resources": "scripts/07_resources.sh",
        "services": "scripts/08_services.sh",
        "logs": "scripts/09_logs.sh",
    }

    def __init__(
        self,
        *,
        root: str = "/opt/vps_sysops",
        profile: str = "aws",
        timeout_seconds: float = 15.0,
        max_output_chars: int = 3500,
    ) -> None:
        self.root = Path(root).expanduser()
        self.profile = profile.strip()
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    async def run(self, operation: str) -> VpsSysopsResult:
        operation = operation.strip().lower()
        relative_script = self.OPERATIONS.get(operation)
        if relative_script is None:
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"不支持的 vps_sysops 操作: {operation or '(empty)'}",
            )

        script = self.root / relative_script
        if not self.root.is_dir():
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"vps_sysops 未部署: {self.root}",
            )
        if not script.is_file():
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"vps_sysops 脚本不存在: {relative_script}",
            )

        env = os.environ.copy()
        if self.profile:
            env["VPS_PROFILE"] = self.profile

        try:
            process = await asyncio.create_subprocess_exec(
                "bash",
                str(script),
                cwd=str(self.root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"vps_sysops 操作超时（>{self.timeout_seconds:g}s）",
            )
        except OSError as exc:
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"无法启动 vps_sysops: {exc}",
            )

        output = _clean_output(stdout.decode("utf-8", errors="replace"))
        if len(output) > self.max_output_chars:
            output = output[: self.max_output_chars].rstrip() + "\n…（输出已截断）"
        return VpsSysopsResult(
            operation=operation,
            ok=process.returncode == 0,
            output=output,
            error="" if process.returncode == 0 else f"脚本退出码 {process.returncode}",
            returncode=process.returncode,
        )


def format_vps_sysops_result(result: VpsSysopsResult) -> str:
    labels = {
        "status": "系统概览",
        "resources": "资源监控",
        "services": "服务状态",
        "logs": "服务日志",
    }
    title = labels.get(result.operation, result.operation)
    mark = "✅" if result.ok else "⚠️"
    body = result.output.strip() or result.error or "无输出"
    if not result.ok and result.error and result.output.strip():
        body = f"{result.error}\n{body}"
    return f"🖥️ vps_sysops · {title} {mark}\n{body}"


def _clean_output(value: str) -> str:
    return _ANSI_RE.sub("", value).replace("\r\n", "\n").strip()
