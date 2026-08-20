from __future__ import annotations

from pathlib import Path

from tools.vps_sysops import VpsSysopsAdapter


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
