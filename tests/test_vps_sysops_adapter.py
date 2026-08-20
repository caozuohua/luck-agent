from __future__ import annotations

from pathlib import Path

from core.operation_policy import OperationPermissionPolicy
from core.targets import VpsTarget, VpsTargetRegistry
from tools.vps_sysops import (
    VpsSysopsAdapter,
    VpsSysopsResult,
    _paginate_output,
    _truncate_output,
)


class _FakeProcess:
    returncode = 123

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"partial log report\n", b""


async def _fake_create_subprocess_exec(*args, **kwargs) -> _FakeProcess:
    return _FakeProcess()


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


async def test_arbitrary_service_restart_is_rejected(tmp_path: Path) -> None:
    adapter = VpsSysopsAdapter(root=str(tmp_path))

    result = await adapter.restart_service("shell rm -rf /")

    assert result.ok is False
    assert "不支持重启服务" in result.error


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


async def test_unauthorized_target_is_rejected_before_transport(tmp_path: Path) -> None:
    local = VpsTarget(provider="aws", target_id="aws-local")
    registry = VpsTargetRegistry.from_csv(
        "gcp-01|gcp|||personal|gcp-ts|caozuohua99|22",
        default_target=local,
    )
    registry.select("user-1", "gcp-01")
    adapter = VpsSysopsAdapter(
        root=str(tmp_path),
        target=local,
        target_registry=registry,
        permission_policy=OperationPermissionPolicy.from_csv(targets="aws-local"),
    )

    result = await adapter.run("resources", user_id="user-1")

    assert result.ok is False
    assert "目标未授权" in result.error


async def test_log_report_with_known_partial_exit_is_classified_as_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = tmp_path / "scripts" / "09_logs.sh"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(
        "tools.vps_sysops.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = await VpsSysopsAdapter(root=str(tmp_path)).run("logs")

    assert result.status == "partial"
    assert result.ok is False
    assert result.returncode == 123
    assert "部分读取受限" in result.error


def test_long_output_keeps_head_and_tail() -> None:
    output, truncated = _truncate_output("HEAD\n" + ("x" * 100) + "\nTAIL", 40)

    assert truncated is True
    assert len(output) <= 40
    assert output.startswith("HEAD")
    assert output.endswith("TAIL")
    assert "已保留首尾" in output


def test_log_output_is_split_into_bounded_pages() -> None:
    pages, complete = _paginate_output("0123456789" * 5, 10)

    assert pages == ("0123456789",) * 5
    assert complete is True


def test_log_pagination_has_a_hard_page_limit() -> None:
    pages, complete = _paginate_output("x" * 130, 10, max_pages=3)

    assert len(pages) == 3
    assert complete is False


def test_result_exposes_stable_status_and_secret_free_dict() -> None:
    result = VpsSysopsResult(
        operation="logs",
        ok=False,
        output="readable logs",
        error="部分读取受限",
        returncode=123,
        partial=True,
        truncated=True,
    )

    assert result.status == "partial"
    assert result.as_dict() == {
        "operation": "logs",
        "status": "partial",
        "ok": False,
        "partial": True,
        "output": "readable logs",
        "error": "部分读取受限",
        "returncode": 123,
        "truncated": True,
        "target": None,
    }
