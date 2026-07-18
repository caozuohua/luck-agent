from __future__ import annotations

import pytest

from core.result_summarizer import ResultSummarizer
from tools.base import ToolResult


@pytest.mark.asyncio
async def test_summarize_success_in_english() -> None:
    message = await ResultSummarizer().summarize(
        ToolResult.ok(data={"value": "done"}, tool_name="tool"),
        user_intent="run task",
        user_language="en",
    )

    assert message == "Completed: run task. Result: done"


@pytest.mark.asyncio
async def test_summarize_error_in_chinese() -> None:
    message = await ResultSummarizer().summarize(
        ToolResult.fail(error="失败", tool_name="tool"),
        user_intent="执行任务",
        user_language="zh",
    )

    assert "未能完成" in message
    assert "失败" in message
