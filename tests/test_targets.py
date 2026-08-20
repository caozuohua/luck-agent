from __future__ import annotations

from core.targets import VpsTarget
from tools.vps_status import HostStatus, format_host_status
from tools.vps_sysops import VpsSysopsResult, format_vps_sysops_result


def test_vps_target_has_stable_display_identity() -> None:
    target = VpsTarget(
        provider="AWS",
        account="prod",
        region="us-east-1",
        target_id="agent-01",
        role="PROD",
    )

    assert target.provider == "aws"
    assert target.role == "prod"
    assert target.display == "AWS / agent-01 / us-east-1"
    assert target.as_dict()["account"] == "prod"


def test_status_renderers_include_target_when_available() -> None:
    target = VpsTarget(provider="gcp", target_id="gcp-01", region="us-west1")
    status = HostStatus(
        hostname="gcp-01",
        platform="Linux",
        uptime_seconds=None,
        load_1m=None,
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        collected_at=0,
        target=target,
    )
    assert "GCP / gcp-01 / us-west1" in format_host_status(status)

    result = VpsSysopsResult(operation="status", ok=True, output="ok", target=target)
    assert "GCP / gcp-01 / us-west1" in format_vps_sysops_result(result)
