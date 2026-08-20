from __future__ import annotations

from typing import Any

from core.operation_policy import operation_description, operation_requires_approval
from core.tool_executor import ToolExecutor
from interface.lark_approval import LarkApprovalManager
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


class RestartTool(Tool):
    name = "service_restart"
    description = "restart a named service"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        return ToolResult.ok(data={"service": kwargs.get("service", "agent")})


async def test_mutating_tool_requires_grant_and_writes_audit() -> None:
    tool = RestartTool()
    registry = ToolRegistry([tool])
    audits: list[dict[str, Any]] = []

    async def audit_writer(**record: Any) -> None:
        audits.append(record)

    executor = ToolExecutor(
        registry,
        approval_checker=lambda user_id, token, tool_name, args: token == "approved",
        audit_writer=audit_writer,
    )

    blocked = await executor.execute(
        "service_restart",
        {"service": "luck-agent"},
        user_id="ou_user",
    )
    assert blocked.error == "APPROVAL_REQUIRED"
    assert tool.calls == 0

    allowed = await executor.execute(
        "service_restart",
        {"service": "luck-agent"},
        user_id="ou_user",
        approval_token="approved",
    )
    assert allowed.ok
    assert tool.calls == 1
    await executor.drain_pending_audits()

    assert [row["decision"] for row in audits] == ["denied", "approved", "executed"]
    assert audits[-1]["details"].startswith("status=ok")


def test_operation_policy_does_not_log_shell_arguments() -> None:
    assert operation_requires_approval("shell", {"command": "systemctl restart luck-agent"})
    assert operation_description(
        "shell",
        {"command": "curl -H 'Authorization: Bearer secret' -X POST https://example"},
    ) == "shell:curl"


def test_lark_grant_is_consumed_once() -> None:
    manager = LarkApprovalManager()
    pending = manager.issue(user_id="ou_user", request="重启服务")

    assert manager.confirm(user_id="ou_user", token=pending.token) == pending
    assert manager.consume_grant("ou_user", pending.token, "service_restart", {})
    assert not manager.consume_grant("ou_user", pending.token, "service_restart", {})
