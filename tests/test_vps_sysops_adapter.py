from __future__ import annotations

from pathlib import Path

from core.targets import VpsTarget, VpsTargetRegistry
from tools.vps_sysops import VpsSysopsAdapter, _truncate_output


async def test_missing_vps_sysops_root_is_reported(tmp_path: Path) -> None:
    adapter = VpsSysopsAdapter(root=str(tmp_path / "missing"))

    result = await adapter.run("status")

    assert result.ok is False
    assert "未部署" in result.error


async def test_arbitrary_operation_is_rejected(tmp_path: Path) -> None:
    adapter = VpsSysopsAdapter(root=str(tmp_path))

    result = await adapter.run("shell rm -rf /")

    assert result.ok is False
    assert "不支持" in result.error


async def test_unconfigured_remote_target_is_rejected_instead_of_running_locally(
    tmp_path: Path,
) -> None:
    local = VpsTarget(provider="aws", target_id="aws-local")
    registry = VpsTargetRegistry.from_csv(
        "gcp-01|gcp|||personal",
        default_target=local,
    )
    registry.select("user-1", "gcp-01")
    adapter = VpsSysopsAdapter(
        root=str(tmp_path),
        target=local,
        target_registry=registry,
    )

    result = await adapter.run("resources", user_id="user-1")

    assert result.ok is False
    assert "SSH" in result.error
    assert result.target is not None
    assert result.target.label == "gcp-01"


def test_long_output_keeps_head_and_tail() -> None:
    output, truncated = _truncate_output("HEAD\n" + ("x" * 100) + "\nTAIL", 40)

    assert truncated is True
    assert len(output) <= 40
    assert output.startswith("HEAD")
    assert output.endswith("TAIL")
    assert "已保留首尾" in output
