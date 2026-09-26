from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from core.graph.executor import GraphExecutionRequest, GraphGoalExecutor
from core.graph.nodes import supervisor_node
from core.operation_context import OperationContext, current_operation, operation_context
from core.supervisor import Supervisor
from core.tool_executor import ToolExecutor
from interface.lark_commands import QuickCommandRouter
from memory.db import Database
from memory.operation_store import OperationStore
from tools.base import ToolResult
from tools.registry import ToolRegistry
from tools.shell import ShellTool


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "receipts.db")
    await db.initialize()
    yield OperationStore(db, deploy_version="test-sha")
    await db.close()


def make_executor(store, run):
    tool = ShellTool()
    tool.run = run
    return ToolExecutor(ToolRegistry([tool]), operation_store=store)


async def invoke(executor, goal="goal", args=None):
    with operation_context(OperationContext(goal_id=goal, run_id=goal + "-run")):
        return await executor.execute("shell", args if args is not None else {"command": "pwd"})


@pytest.mark.asyncio
async def test_receipt_committed_before_effect_and_no_raw_payload(store):
    async def run(**kwargs):
        # A separate connection must see the executing receipt before the tool.
        reader = Database(store.db.path)
        try:
            row = await reader.fetchone("SELECT * FROM operation_attempts")
            assert row["state"] == "executing"
            assert "private-command" not in json.dumps(dict(row))
        finally:
            await reader.close()
        return ToolResult.ok(data="private-output")

    result = await invoke(make_executor(store, run), args={"command": "private-command"})
    row, = await store.list_for_goal("goal")
    assert row["state"] == "returned_ok"  # Never a claim of verified success.
    assert row["attempt_id"] == result.metadata["attempt_id"]
    assert "private-output" not in str(row)
    assert json.loads(row["metadata"])["deploy_version"] == "test-sha"


@pytest.mark.asyncio
async def test_invalid_call_has_rejected_receipt_without_execution(store):
    run = AsyncMock()
    result = await invoke(make_executor(store, run), args={})
    assert result.error == "INVALID_TOOL_ARGUMENTS"
    row, = await store.list_for_goal("goal")
    assert row["state"] == "rejected"
    assert row["started_at"] is None
    run.assert_not_called()


@pytest.mark.asyncio
async def test_audit_failure_prevents_execution_and_approval_consumption(store, monkeypatch):
    run = AsyncMock()
    executor = make_executor(store, run)
    executor.approval_checker = Mock(return_value=True)
    monkeypatch.setattr(store, "prepare", AsyncMock(side_effect=OSError()))
    result = await invoke(executor)
    assert result.error == "OPERATION_AUDIT_UNAVAILABLE"
    run.assert_not_called()
    executor.approval_checker.assert_not_called()


@pytest.mark.asyncio
async def test_result_persistence_failure_never_returns_success(store, monkeypatch):
    original = store.db.execute

    async def execute(sql, parameters=()):
        if "finished_at=?" in sql:
            raise OSError("disk full")
        return await original(sql, parameters)

    monkeypatch.setattr(store.db, "execute", execute)
    result = await invoke(make_executor(store, AsyncMock(return_value=ToolResult.ok())))
    assert result.error == "OPERATION_RESULT_NOT_PERSISTED"
    assert result.metadata["execution_uncertain"]
    assert len(await store.unresolved_writes("goal")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "exception", "cancel"])
async def test_interrupted_effect_survives_reopen_as_unknown(store, mode):
    entered = asyncio.Event()

    async def run(**kwargs):
        entered.set()
        if mode == "exception":
            raise RuntimeError("private-exception")
        await asyncio.Event().wait()

    executor = make_executor(store, run)
    executor.timeout_seconds = .05
    task = asyncio.create_task(invoke(executor))
    await entered.wait()
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.metadata["execution_uncertain"]
        assert result.metadata["blocking"]
    await store.db.close()
    row, = await store.unresolved_writes("goal")
    assert row["state"] == "unknown"
    assert "private-exception" not in str(row)


@pytest.mark.asyncio
async def test_terminal_receipt_cannot_be_reexecuted_or_rewritten(store):
    attempt = await store.prepare(context=OperationContext(), user_id="u", requested_tool="x",
                                  tool_name="x", arguments={})
    await attempt.executing()
    await attempt.finish(state="returned_ok")
    with pytest.raises(RuntimeError):
        await attempt.executing()
    with pytest.raises(RuntimeError):
        await attempt.finish(state="unknown")


def graph_executor(tool_executor):
    return GraphGoalExecutor(llm_client=None, tool_registry=None, tool_executor=tool_executor,
                             supervisor=None, prompt_builder=None, output_parser=None,
                             intent_classifier=None, router=None, graph_db_path="unused")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["executing", "unknown", "returned_ok", "returned_error"])
async def test_graph_restart_does_not_replay_unresolved_write(store, monkeypatch, state):
    attempt = await store.prepare(context=OperationContext(goal_id="g"), user_id="u",
                                  requested_tool="x", tool_name="x", arguments={})
    await attempt.executing()
    if state != "executing":
        await attempt.finish(state=state)
    run_graph = AsyncMock()
    monkeypatch.setattr("core.graph.executor.run_graph", run_graph)
    result = await graph_executor(SimpleNamespace(operation_store=store)).execute(
        GraphExecutionRequest("g", "u", "restart"))
    assert result["decision"] == "fail"
    run_graph.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_graph_contexts_do_not_leak(monkeypatch):
    captured = []

    async def run_graph(state, **kwargs):
        before = current_operation.get()
        await asyncio.sleep(0)
        assert before == current_operation.get()
        captured.append(before)
        return state

    monkeypatch.setattr("core.graph.executor.run_graph", run_graph)
    executor = graph_executor(object())
    await asyncio.gather(*(executor.execute(GraphExecutionRequest(str(i), "u", "x", chat_id=str(i)))
                           for i in range(5)))
    assert {c.goal_id for c in captured} == {str(i) for i in range(5)}
    assert len({c.run_id for c in captured}) == 5
    assert all(c.chat_id == c.goal_id for c in captured)
    assert current_operation.get() == OperationContext()


@pytest.mark.asyncio
async def test_unknown_outcome_cannot_be_overridden_by_model_done_or_approval():
    result = await supervisor_node({
        "last_parsed": {"intent": "DONE", "message": "success"}, "decision": "done",
        "last_tool_result": ToolResult.fail(error="timeout", metadata={"execution_uncertain": True}).to_dict(),
    }, supervisor=Supervisor(), goal={}, max_retry=99, hitl=True)
    assert result["decision"] == "fail"
    assert not result["is_goal_complete"]


@pytest.mark.asyncio
async def test_quick_write_internal_typeerror_is_not_retried(store):
    write = AsyncMock(side_effect=TypeError("internal user_id failure after write"))
    router = QuickCommandRouter(health=None, vps=None, sysops=SimpleNamespace(restart_service=write),
                                approval_checker=lambda *a: True, operation_store=store)
    response = await router._service_operation("new-api", "restart", "u", approval_token="secret")
    assert "尚未确认" in response
    write.assert_awaited_once()
    row, = await store.unresolved_writes("")
    assert row["state"] == "unknown"
    assert row["approved"] == 1
    assert "secret" not in str(row)


@pytest.mark.asyncio
async def test_quick_timeout_result_is_durable_unknown(store):
    from tools.vps_sysops import VpsSysopsResult

    write = AsyncMock(return_value=VpsSysopsResult(operation="restart", ok=False,
                                                  execution_uncertain=True))
    router = QuickCommandRouter(health=None, vps=None, sysops=SimpleNamespace(restart_service=write),
                                approval_checker=lambda *a: True, operation_store=store)
    response = await router.handle("/vps service new-api restart", user_id="u", chat_id="chat",
                                   approval_token="secret")
    assert "尚未确认" in response
    row, = await store.unresolved_writes("")
    assert row["state"] == "unknown"
    metadata = json.loads(row["metadata"])
    assert metadata["chat_id"] == "chat"
    assert metadata["request_id"] and metadata["run_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_sysops_write_transport_failure_reports_uncertainty(monkeypatch, timeout):
    from tools.vps_sysops import VpsSysopsAdapter

    process = SimpleNamespace(returncode=255, kill=Mock(), wait=AsyncMock(),
                              communicate=AsyncMock(return_value=(b"transport failed", b"")))
    if timeout:
        process.communicate.side_effect = TimeoutError
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await VpsSysopsAdapter().restart_service("luck-agent")
    assert not result.ok
    assert result.execution_uncertain
    if timeout:
        process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_quick_audit_failure_does_not_consume_approval(store, monkeypatch):
    write, approval = AsyncMock(), Mock(return_value=True)
    monkeypatch.setattr(store, "prepare", AsyncMock(side_effect=OSError()))
    router = QuickCommandRouter(health=None, vps=None, sysops=SimpleNamespace(restart_service=write),
                                approval_checker=approval, operation_store=store)
    response = await router._service_operation("new-api", "restart", "u", approval_token="secret")
    assert "未执行" in response
    write.assert_not_called()
    approval.assert_not_called()
