from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

AssetType = Literal["systemd", "user_systemd", "docker", "port", "cron", "mount"]
AssetStatus = Literal["active", "inactive", "unknown"]


@dataclass(frozen=True)
class ServiceAsset:
    asset_type: AssetType
    service_id: str
    label: str
    status: AssetStatus = "unknown"
    ports: tuple[int, ...] = ()
    depends_on: tuple[str, ...] = ()
    backup_location: str = ""
    health_check_cmd: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_assets(payload: str) -> tuple[ServiceAsset, ...]:
    """Parse the intentionally small JSON contract emitted by the remote script."""
    raw: Any = json.loads(payload or "[]")
    if isinstance(raw, dict):
        raw = raw.get("assets", [])
    if not isinstance(raw, list):
        raise ValueError("资产发现结果必须是数组")
    result: list[ServiceAsset] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ports = tuple(int(p) for p in item.get("ports", []) if str(p).isdigit())
        result.append(ServiceAsset(
            asset_type=item.get("asset_type", "unknown"), service_id=str(item.get("service_id", "")),
            label=str(item.get("label", item.get("service_id", ""))),
            status=item.get("status", "unknown"), ports=ports,
            depends_on=tuple(str(x) for x in item.get("depends_on", [])),
            backup_location=str(item.get("backup_location", "")),
            health_check_cmd=str(item.get("health_check_cmd", "")),
            metadata={str(k): str(v) for k, v in dict(item.get("metadata", {})).items()},
        ))
    return tuple(result)


def format_assets(assets: tuple[ServiceAsset, ...]) -> str:
    if not assets:
        return "未发现运行态资产"
    groups: dict[str, list[str]] = {}
    for asset in assets:
        detail = f"{asset.label}（{asset.status}）"
        if asset.ports:
            detail += f" 端口:{','.join(map(str, asset.ports))}"
        groups.setdefault(asset.asset_type, []).append(detail)
    names = {"systemd": "systemd", "user_systemd": "用户 systemd", "docker": "Docker", "port": "监听端口", "cron": "Cron", "mount": "挂载"}
    return "\n\n".join(f"**{names.get(kind, kind)}**\n" + "\n".join(f"• {x}" for x in values) for kind, values in groups.items())


__all__ = ["ServiceAsset", "parse_assets", "format_assets"]
