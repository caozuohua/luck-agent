from __future__ import annotations

import asyncio
import math
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from core.operation_policy import OperationPermissionPolicy
from core.targets import VpsTarget, VpsTargetRegistry


_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


@dataclass(frozen=True)
class VpsSysopsResult:
    operation: str
    ok: bool
    output: str = ""
    error: str = ""
    returncode: int | None = None
    target: VpsTarget | None = None
    truncated: bool = False
    partial: bool = False

    @property
    def status(self) -> str:
        """Return a stable result status for cards, audit, and API callers."""
        if self.partial:
            return "partial"
        return "ok" if self.ok else "error"

    def as_dict(self) -> dict[str, object]:
        """Expose a secret-free, machine-readable result contract."""
        return {
            "operation": self.operation,
            "status": self.status,
            "ok": self.ok,
            "partial": self.partial,
            "output": self.output,
            "error": self.error,
            "returncode": self.returncode,
            "truncated": self.truncated,
            "target": self.target.as_dict() if self.target is not None else None,
        }


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
    SERVICE_PROBES: dict[str, str] = {
        "a2a": "curl -fsS --max-time 5 http://127.0.0.1:8765/.well-known/agent-card.json",
    }

    def __init__(
        self,
        *,
        root: str = "/opt/vps_sysops",
        profile: str = "aws",
        target: VpsTarget | None = None,
        target_registry: VpsTargetRegistry | None = None,
        permission_policy: OperationPermissionPolicy | None = None,
        ssh_config: str = "",
        ssh_identity_file: str = "",
        timeout_seconds: float = 15.0,
        max_output_chars: int = 3500,
    ) -> None:
        self.root = Path(root).expanduser()
        self.profile = profile.strip()
        self.target = target
        self.target_registry = target_registry
        self.permission_policy = permission_policy
        self.ssh_config = ssh_config.strip()
        self.ssh_identity_file = ssh_identity_file.strip()
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    async def run(self, operation: str, *, user_id: str = "default") -> VpsSysopsResult:
        target = self.target_registry.current(user_id) if self.target_registry else self.target
        operation = operation.strip().lower()
        relative_script = self.OPERATIONS.get(operation)
        if relative_script is None:
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"不支持的 vps_sysops 操作: {operation or '(empty)'}",
                target=target,
            )

        if target is not None and self.permission_policy is not None:
            if not self.permission_policy.allows_target(target.label):
                return VpsSysopsResult(
                    operation=operation,
                    ok=False,
                    error=f"目标未授权：{target.label}",
                    target=target,
                )

        is_local = self._is_local_target(target)
        if is_local is None:
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=(
                    f"目标未配置 SSH 运维通道: {target.display if target else '(unknown)'}；"
                    "请配置 VPS_TARGETS 的 ssh_host/ssh_user"
                ),
                target=target,
            )
        script_root = self.root
        if not is_local and target is not None and target.sysops_root:
            script_root = Path(target.sysops_root).expanduser()
        script = script_root / relative_script
        if is_local and not self.root.is_dir():
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"vps_sysops 未部署: {self.root}",
                target=target,
            )
        if is_local and not script.is_file():
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"vps_sysops 脚本不存在: {relative_script}",
                target=target,
            )

        env = self._build_environment(target, is_local=is_local)
        command, cwd = self._build_command(
            script=script,
            target=target,
            is_local=is_local,
            env=env,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
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
                target=target,
            )
        except OSError as exc:
            return VpsSysopsResult(
                operation=operation,
                ok=False,
                error=f"无法启动 vps_sysops: {exc}",
                target=target,
            )

        output = _clean_output(stdout.decode("utf-8", errors="replace"))
        output, truncated = _truncate_output(output, self.max_output_chars)
        returncode = process.returncode
        error = "" if returncode == 0 else f"脚本退出码 {returncode}"
        partial = (
            operation == "logs"
            and returncode in {1, 123}
            and bool(output)
        )
        if partial:
            error = "日志部分读取受限：当前运维用户无权读取部分系统日志，已返回可读取内容"
        return VpsSysopsResult(
            operation=operation,
            ok=returncode == 0,
            output=output,
            error=error,
            returncode=returncode,
            target=target,
            truncated=truncated,
            partial=partial,
        )

    async def probe_service(self, service: str, *, user_id: str = "default") -> VpsSysopsResult:
        """Run a fixed, read-only service probe on the selected target."""
        service = service.strip().lower()
        target = self.target_registry.current(user_id) if self.target_registry else self.target
        probe = self.SERVICE_PROBES.get(service)
        if probe is None:
            return VpsSysopsResult(
                operation=f"service:{service}",
                ok=False,
                error=f"不支持的服务探针: {service or '(empty)'}",
                target=target,
            )
        if target is not None and self.permission_policy is not None:
            if not self.permission_policy.allows_target(target.label):
                return VpsSysopsResult(
                    operation=f"service:{service}",
                    ok=False,
                    error=f"目标未授权：{target.label}",
                    target=target,
                )
        is_local = self._is_local_target(target)
        if is_local is None:
            return VpsSysopsResult(
                operation=f"service:{service}",
                ok=False,
                error=(
                    f"目标未配置 SSH 运维通道: {target.display if target else '(unknown)'}"
                ),
                target=target,
            )
        env = self._build_environment(target, is_local=is_local)
        command, cwd = self._build_probe_command(
            probe=probe,
            target=target,
            is_local=is_local,
            env=env,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
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
                operation=f"service:{service}",
                ok=False,
                error=f"服务探针超时（>{self.timeout_seconds:g}s）",
                target=target,
            )
        except OSError as exc:
            return VpsSysopsResult(
                operation=f"service:{service}",
                ok=False,
                error=f"无法启动服务探针: {exc}",
                target=target,
            )
        output = _clean_output(stdout.decode("utf-8", errors="replace"))
        output, truncated = _truncate_output(output, self.max_output_chars)
        returncode = process.returncode
        return VpsSysopsResult(
            operation=f"service:{service}",
            ok=returncode == 0,
            output=output,
            error="" if returncode == 0 else f"服务探针退出码 {returncode}",
            returncode=returncode,
            target=target,
            truncated=truncated,
        )

    def _is_local_target(self, target: VpsTarget | None) -> bool | None:
        """Return local/remote execution mode, or None when unroutable."""
        if target is None:
            return True
        if target.ssh_host:
            return False
        if self.target is None or target.label == self.target.label:
            return True
        return None

    def _build_environment(self, target: VpsTarget | None, *, is_local: bool) -> dict[str, str]:
        env = os.environ.copy()
        if is_local:
            if self.profile:
                env["VPS_PROFILE"] = self.profile
        elif target is not None:
            env["VPS_PROFILE"] = target.provider
        if target is not None:
            env.update(
                {
                    "VPS_PROVIDER": target.provider,
                    "VPS_ACCOUNT": target.account,
                    "VPS_REGION": target.region,
                    "VPS_TARGET_ID": target.target_id,
                    "VPS_ROLE": target.role,
                }
            )
        return env

    def _build_command(
        self,
        *,
        script: Path,
        target: VpsTarget | None,
        is_local: bool,
        env: dict[str, str],
    ) -> tuple[list[str], str | None]:
        if is_local:
            return ["bash", str(script)], str(self.root)
        if target is None or not target.ssh_host:
            raise ValueError("remote vps_sysops target requires ssh_host")
        destination = target.ssh_host
        if target.ssh_user:
            destination = f"{target.ssh_user}@{destination}"
        ssh_args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, math.ceil(self.timeout_seconds))}",
        ]
        if self.ssh_config:
            ssh_args.extend(["-F", self.ssh_config])
        if self.ssh_identity_file:
            ssh_args.extend(["-i", self.ssh_identity_file])
        if target.ssh_port != 22:
            ssh_args.extend(["-p", str(target.ssh_port)])
        remote_env = " ".join(
            f"{key}={shlex.quote(env[key])}"
            for key in (
                "VPS_PROFILE",
                "VPS_PROVIDER",
                "VPS_ACCOUNT",
                "VPS_REGION",
                "VPS_TARGET_ID",
                "VPS_ROLE",
            )
            if key in env
        )
        remote_command = f"{remote_env} bash {shlex.quote(str(script))}".strip()
        return [*ssh_args, destination, remote_command], None

    def _build_probe_command(
        self,
        *,
        probe: str,
        target: VpsTarget | None,
        is_local: bool,
        env: dict[str, str],
    ) -> tuple[list[str], str | None]:
        if is_local:
            return ["bash", "-lc", probe], str(self.root)
        if target is None or not target.ssh_host:
            raise ValueError("remote service probe requires ssh_host")
        destination = target.ssh_host
        if target.ssh_user:
            destination = f"{target.ssh_user}@{destination}"
        ssh_args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, math.ceil(self.timeout_seconds))}",
        ]
        if self.ssh_config:
            ssh_args.extend(["-F", self.ssh_config])
        if self.ssh_identity_file:
            ssh_args.extend(["-i", self.ssh_identity_file])
        if target.ssh_port != 22:
            ssh_args.extend(["-p", str(target.ssh_port)])
        remote_env = " ".join(
            f"{key}={shlex.quote(env[key])}"
            for key in (
                "VPS_PROFILE",
                "VPS_PROVIDER",
                "VPS_ACCOUNT",
                "VPS_REGION",
                "VPS_TARGET_ID",
                "VPS_ROLE",
            )
            if key in env
        )
        return [*ssh_args, destination, f"{remote_env} {probe}".strip()], None


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
    target_line = f"\n• 目标：`{result.target.display}`" if result.target else ""
    if not result.ok and result.error and result.output.strip():
        body = f"{result.error}\n{body}"
    return f"🖥️ vps_sysops · {title} {mark}{target_line}\n{body}"


def _clean_output(value: str) -> str:
    return _ANSI_RE.sub("", value).replace("\r\n", "\n").strip()


def _truncate_output(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n…（输出较长，已保留首尾）…\n"
    if limit <= len(marker) + 2:
        return value[:limit], True
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return value[:head].rstrip() + marker + value[-tail:].lstrip(), True
