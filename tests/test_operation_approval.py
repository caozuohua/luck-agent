from __future__ import annotations

from typing import Any

from core.operation_policy import (
    OperationPermissionPolicy,
    scope_from_request,
    scope_from_tool,
    operation_description,
    operation_requires_approval,
)
from core.tool_executor import ToolExecutor
from interface.lark_approval import LarkApprovalManager
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


def test_new_api_backup_command_requires_confirmation() -> None:
    manager = LarkApprovalManager()

    assert manager.requires_confirmation("/vps service new-api backup")


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


async def test_operation_permission_blocks_out_of_scope_target() -> None:
    tool = RestartTool()
    audits: list[dict[str, Any]] = []

    async def audit_writer(**record: Any) -> None:
        audits.append(record)

    policy = OperationPermissionPolicy.from_csv(
        targets="aws-prod",
        services="luck-agent",
        operations="restart",
    )
    executor = ToolExecutor(
        ToolRegistry([tool]),
        permission_checker=lambda user_id, tool_name, args: policy.allows(tool_name, args),
        audit_writer=audit_writer,
    )

    result = await executor.execute(
        "service_restart",
        {"target": "gcp-test", "service": "luck-agent"},
        user_id="ou_user",
    )

    assert result.error == "PERMISSION_DENIED"
    assert tool.calls == 0
    await executor.drain_pending_audits()
    assert audits[0]["decision"] == "permission_denied"


def test_operation_policy_does_not_log_shell_arguments() -> None:
    assert operation_requires_approval("shell", {"command": "systemctl restart luck-agent"})
    assert operation_description(
        "shell",
        {"command": "curl -H 'Authorization: Bearer secret' -X POST https://example"},
    ) == "shell:curl"


def test_operation_permission_policy_matches_target_service_and_operation() -> None:
    policy = OperationPermissionPolicy.from_csv(
        targets="aws-prod",
        services="luck-agent",
        operations="restart",
    )

    assert policy.allows(
        "shell",
        {
            "target": "aws-prod",
            "command": "systemctl restart luck-agent",
        },
    )
    assert not policy.allows(
        "shell",
        {
            "target": "aws-test",
            "command": "systemctl restart luck-agent",
        },
    )
    assert policy.allows("web_search", {"query": "systemd"})
    assert not policy.allows(
        "shell",
        {
            "target": "aws-prod",
            "command": "systemctl stop luck-agent",
        },
    )


def test_operation_permission_policy_applies_target_allowlist_to_read_commands() -> None:
    policy = OperationPermissionPolicy.from_csv(targets="gcp-01, azure-01")

    assert policy.allows_target("gcp-01") is True
    assert policy.allows_target("AWS-01") is False
    assert policy.allows("vps_resources", {"target": "aws-01"}) is False


def test_operation_permission_policy_applies_service_allowlist() -> None:
    policy = OperationPermissionPolicy.from_csv(services="mem0,a2a")

    assert policy.allows_service("Mem0") is True
    assert policy.allows_service("new-api") is False


def test_operation_permission_policy_applies_operation_allowlist() -> None:
    policy = OperationPermissionPolicy.from_csv(operations="restart")

    assert policy.allows_operation("restart") is True
    assert policy.allows_operation("deploy") is False


def test_operation_permission_policy_applies_user_allowlist_only_to_ops() -> None:
    policy = OperationPermissionPolicy.from_csv(user_ids="ou_ops")

    assert policy.allows_user("OU_OPS") is True
    assert policy.allows_user("ou_viewer") is False
    assert policy.allows("vps_resources", {"target": "aws-01"}, user_id="ou_ops")
    assert not policy.allows("vps_resources", {"target": "aws-01"}, user_id="ou_viewer")
    assert policy.allows("web_search", {"query": "systemd"}, user_id="ou_viewer")


def test_lark_grant_is_consumed_once() -> None:
    manager = LarkApprovalManager()
    pending = manager.issue(user_id="ou_user", request="重启服务")

    assert manager.confirm(user_id="ou_user", token=pending.token) == pending
    assert manager.consume_grant("ou_user", pending.token, "service_restart", {})
    assert not manager.consume_grant("ou_user", pending.token, "service_restart", {})


def test_confirmation_scope_matches_operation_target_and_service() -> None:
    manager = LarkApprovalManager()
    pending = manager.issue(
        user_id="ou_user",
        request="重启 target=aws-prod 的 luck-agent 服务",
    )
    assert pending.scope == scope_from_request("重启 target=aws-prod 的 luck-agent 服务")
    assert manager.confirm(user_id="ou_user", token=pending.token) == pending

    assert manager.consume_grant(
        "ou_user",
        pending.token,
        "service_restart",
        {"target": "aws-prod", "service": "luck-agent"},
    )

    pending = manager.issue(
        user_id="ou_user",
        request="重启 target=aws-prod 的 luck-agent 服务",
    )
    manager.confirm(user_id="ou_user", token=pending.token)
    assert not manager.consume_grant(
        "ou_user",
        pending.token,
        "service_stop",
        {"target": "aws-prod", "service": "luck-agent"},
    )


def test_confirmation_scope_omitted_fields_are_wildcards() -> None:
    manager = LarkApprovalManager()
    pending = manager.issue(user_id="ou_user", request="重启服务")
    manager.confirm(user_id="ou_user", token=pending.token)

    assert manager.consume_grant(
        "ou_user",
        pending.token,
        "service_restart",
        {"target": "aws-prod", "service": "luck-agent"},
    )
    assert scope_from_tool("service_restart", {"service": "luck-agent"}).operation == "restart"


def test_confirmation_scope_parses_vps_service_command() -> None:
    scope = scope_from_request("/vps service luck-agent restart")

    assert scope.operation == "restart"
    assert scope.service == "luck-agent"


def test_confirmation_scope_parses_explicit_mem0_write() -> None:
    manager = LarkApprovalManager()
    request = "/mem0 save 用户偏好"

    assert manager.requires_confirmation(request)
    pending = manager.issue(user_id="ou_user", request=request)
    assert pending.scope == scope_from_request(request)
    assert pending.scope.operation == "write"
    assert pending.scope.service == "mem0"
    manager.confirm(user_id="ou_user", token=pending.token)

    assert manager.consume_grant(
        "ou_user",
        pending.token,
        "memory_write",
        {"service": "mem0", "operation": "write"},
    )


def test_natural_language_memory_request_is_not_an_operation_confirmation() -> None:
    manager = LarkApprovalManager()

    assert not manager.requires_confirmation("请记住我喜欢简洁回答")
    assert manager.requires_confirmation("/mem0 save 我喜欢简洁回答")
