from __future__ import annotations

import asyncio

import pytest

from tools.shell import ShellTool


class FakeStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self) -> bytes:
        return self.payload


class FakeProcess:
    def __init__(self, stdout: bytes = b"out", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self.returncode = returncode
        self.killed = False

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_shell_runs_allowed_command_in_locked_workdir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: list[dict] = []

    async def fake_create(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return FakeProcess(stdout=b"hello\n")

    monkeypatch.setenv("AGENT_WORKDIR", str(tmp_path))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    result = await ShellTool().run("echo hello")

    assert result.status == "ok"
    assert result.data["output"] == "hello\n"
    assert calls[0]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_shell_rejects_disallowed_prefix_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess should not be created")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    result = await ShellTool().run("python -V")

    assert result.status == "error"
    assert "not allowed" in result.error


@pytest.mark.asyncio
async def test_shell_rejects_high_risk_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("subprocess should not be created")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    result = await ShellTool().run("sudo ls")

    assert result.status == "error"
    assert "dangerous" in result.error


@pytest.mark.asyncio
async def test_shell_timeout_kills_process(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    process = FakeProcess(stdout=b"", stderr=b"")

    async def never_wait() -> int:
        await asyncio.sleep(1)
        return 0

    process.wait = never_wait

    async def fake_create(command, **kwargs):
        return process

    monkeypatch.setenv("AGENT_WORKDIR", str(tmp_path))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    result = await ShellTool().run("ls", timeout=0.01)

    assert result.status == "error"
    assert result.error == "TIMEOUT_ERROR"
    assert process.killed


@pytest.mark.asyncio
async def test_shell_truncates_combined_output(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    async def fake_create(command, **kwargs):
        return FakeProcess(stdout=b"a" * 4100, stderr=b"b" * 50)

    monkeypatch.setenv("AGENT_WORKDIR", str(tmp_path))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create)

    result = await ShellTool().run("ls")

    assert result.status == "ok"
    assert len(result.data["output"]) <= 4012
    assert "[truncated]" in result.data["output"]
