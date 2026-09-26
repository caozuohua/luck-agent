from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from core.capabilities import local_inventory_answer
from core.graph.executor import GraphExecutionRequest, GraphGoalExecutor
from core.graph.nodes import supervisor_node
from core.supervisor import Supervisor
from core.tool_executor import ToolExecutor
from tools.base import ToolResult
from tools.registry import ToolRegistry
from tools.shell import ShellTool
from tools.web_search import WebSearchTool


@pytest.mark.asyncio
async def test_missing_configuration_prevents_network_and_approval(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    tool = WebSearchTool()
    tool.run = AsyncMock()
    approval = Mock()
    result = await ToolExecutor(ToolRegistry([tool]), approval_checker=approval).execute(
        "web_search", {"query": "weather"})
    assert result.error == "TOOL_UNAVAILABLE"
    assert result.metadata["executed"] is False
    assert result.metadata["error_class"] == "configuration"
    tool.run.assert_not_called()
    approval.assert_not_called()
    state = await supervisor_node({"step_count": 0, "last_parsed": {"intent": "ACTION"},
                                   "last_tool_result": result.to_dict()},
                                  supervisor=Supervisor(), goal={}, max_retry=99)
    assert state["decision"] == "fail"


@pytest.mark.asyncio
async def test_configuration_is_refreshed_without_echoing_secret(monkeypatch):
    tool = WebSearchTool()
    tool.run = AsyncMock(return_value=ToolResult.ok())
    registry = ToolRegistry([tool])
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    assert not registry.capability_snapshot()[0]["locally_ready"]
    monkeypatch.setenv("SERPER_API_KEY", "sensitive-key-do-not-echo")
    assert registry.capability_snapshot()[0]["locally_ready"]
    assert "sensitive-key" not in str(registry.capability_snapshot())
    result = await ToolExecutor(registry).execute("web_search", {"query": "weather"})
    assert result.status == "ok"
    tool.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_broken_availability_check_fails_closed_without_exception_contents(monkeypatch):
    tool = ShellTool()
    monkeypatch.setattr(tool, "availability", Mock(side_effect=RuntimeError("secret-value")))
    tool.run = AsyncMock()
    registry = ToolRegistry([tool])
    result = await ToolExecutor(registry).execute("shell", {"command": "pwd"})
    assert result.error == "TOOL_UNAVAILABLE"
    assert "secret-value" not in str(result.to_dict()) + str(registry.capability_snapshot())
    tool.run.assert_not_called()


@pytest.mark.parametrize("text", ["现在有什么工具、技能、mcp可用的", "你有哪些工具？", "当前工具清单", "本机有哪些能力？"])
def test_local_inventory_queries_are_answered_from_registry(text, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    answer = local_inventory_answer(text, ToolRegistry([WebSearchTool(), ShellTool()]))
    assert answer is not None
    assert "missing_SERPER_API_KEY" in answer
    assert "shell" in answer


@pytest.mark.parametrize("text", ["搜索最新 AI 工具", "你有哪些工具，然后搜索天气", "检查服务是否健康", "帮我安装工具", "工具选择是否正确"])
def test_inventory_does_not_swallow_actions_or_external_questions(text):
    assert local_inventory_answer(text, ToolRegistry()) is None


@pytest.mark.asyncio
async def test_inventory_graph_uses_no_model_or_external_tools(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr("core.graph.executor.run_graph", run)
    executor = GraphGoalExecutor(llm_client=None, tool_registry=ToolRegistry([ShellTool()]),
                                 tool_executor=object(), supervisor=None, prompt_builder=None,
                                 output_parser=None, intent_classifier=None, router=None,
                                 graph_db_path="unused")
    result = await executor.execute(GraphExecutionRequest("g", "u", "你有哪些工具？"))
    assert result["decision"] == "done"
    assert "shell" in result["final_answer"]
    run.assert_not_called()
