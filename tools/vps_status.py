from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostStatus:
    """Read-only status snapshot for the VPS running this agent."""

    hostname: str
    platform: str
    uptime_seconds: float | None
    load_1m: float | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    collected_at: float


class VpsStatusService:
    """Collect local VPS metrics without shell commands or an LLM."""

    def __init__(self, *, name: str = "") -> None:
        self.name = name.strip()

    async def collect(self) -> HostStatus:
        return await asyncio.to_thread(self._collect_sync)

    def _collect_sync(self) -> HostStatus:
        disk_path = Path.cwd().anchor or Path.cwd()
        try:
            disk = shutil.disk_usage(disk_path)
            disk_total = disk.total
            disk_free = disk.free
        except OSError:
            disk_total = disk_free = None

        memory_total, memory_available = self._read_memory()
        uptime = self._read_uptime()
        try:
            load_1m = float(os.getloadavg()[0])
        except (AttributeError, OSError):
            load_1m = None

        return HostStatus(
            hostname=self.name or socket.gethostname(),
            platform=f"{platform.system()} {platform.release()}".strip(),
            uptime_seconds=uptime,
            load_1m=load_1m,
            memory_total_bytes=memory_total,
            memory_available_bytes=memory_available,
            disk_total_bytes=disk_total,
            disk_free_bytes=disk_free,
            collected_at=time.time(),
        )

    def _read_uptime(self) -> float | None:
        try:
            return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        except (FileNotFoundError, OSError, ValueError, IndexError):
            return None

    def _read_memory(self) -> tuple[int | None, int | None]:
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, raw_value = line.partition(":")
                if not _:
                    continue
                parts = raw_value.strip().split()
                if parts and parts[0].isdigit():
                    # Linux meminfo values are kB unless explicitly stated otherwise.
                    values[key] = int(parts[0]) * 1024
            total = values.get("MemTotal")
            available = values.get("MemAvailable")
            if available is None:
                available = values.get("MemFree")
            return total, available
        except (FileNotFoundError, OSError, ValueError):
            return None, None


def format_host_status(status: HostStatus) -> str:
    """Render a compact mobile-friendly status message."""

    lines = [f"🖥️ VPS 状态：✅ 正常", f"• 主机：`{status.hostname}`"]
    if status.uptime_seconds is not None:
        lines.append(f"• 运行：{_format_duration(status.uptime_seconds)}")
    if status.load_1m is not None:
        lines.append(f"• 负载：{status.load_1m:.2f}（1 分钟）")
    memory = _format_memory(status.memory_total_bytes, status.memory_available_bytes)
    if memory:
        lines.append(f"• 内存：{memory}")
    disk = _format_disk(status.disk_total_bytes, status.disk_free_bytes)
    if disk:
        lines.append(f"• 磁盘：{disk}")
    return "\n".join(lines)


def _format_memory(total: int | None, available: int | None) -> str:
    if not total or available is None:
        return ""
    used = max(0, total - available)
    percent = used / total * 100
    return f"{_format_bytes(used)} / {_format_bytes(total)}（{percent:.0f}%）"


def _format_disk(total: int | None, free: int | None) -> str:
    if not total or free is None:
        return ""
    used = max(0, total - free)
    percent = used / total * 100
    return f"{_format_bytes(used)} / {_format_bytes(total)}（{percent:.0f}%）"


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.1f}{unit}"


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"
