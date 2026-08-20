from __future__ import annotations

import pytest

from core.targets import VpsTarget, VpsTargetRegistry
from tools.vps_status import HostStatus, VpsStatusService, format_host_status
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


def test_target_registry_parses_targets_and_keeps_selection_per_user() -> None:
    default = VpsTarget(provider="aws", target_id="aws-01", region="us-east-1")
    registry = VpsTargetRegistry.from_csv(
        "gcp-01|gcp|project-a|us-west1|staging;azure-01|azure||east|prod",
        default_target=default,
    )

    assert [target.label for target in registry.list()] == ["aws-01", "gcp-01", "azure-01"]
    assert registry.current("alice").label == "aws-01"
    assert registry.select("alice", "gcp-01").display == "GCP / gcp-01 / us-west1"
    assert registry.current("alice").label == "gcp-01"
    assert registry.current("bob").label == "aws-01"
    assert registry.select("alice", "missing") is None


async def test_status_does_not_label_local_metrics_as_remote_target() -> None:
    default = VpsTarget(provider="aws", target_id="aws-01")
    registry = VpsTargetRegistry.from_csv(
        "gcp-01|gcp|||personal",
        default_target=default,
    )
    registry.select("alice", "gcp-01")
    service = VpsStatusService(target=default, target_registry=registry)

    with pytest.raises(RuntimeError, match="拒绝返回本机资源"):
        await service.collect(user_id="alice")
