from __future__ import annotations

from typing import Any

from tools.base import ToolResult


class ResultSummarizer:
    async def summarize(
        self,
        tool_result: ToolResult,
        user_intent: str,
        user_language: str,
    ) -> str:
        if user_language == "en":
            return self._summarize_en(tool_result, user_intent)
        return self._summarize_zh(tool_result, user_intent)

    def _summarize_zh(self, tool_result: ToolResult, user_intent: str) -> str:
        if tool_result.status == "error":
            return f"未能完成：{user_intent}。原因：{tool_result.error or '未知错误'}。"
        detail = self._human_detail(tool_result.data)
        if detail:
            return f"已完成：{user_intent}。结果：{detail}"
        return f"已完成：{user_intent}。"

    def _summarize_en(self, tool_result: ToolResult, user_intent: str) -> str:
        if tool_result.status == "error":
            return f"Could not complete: {user_intent}. Reason: {tool_result.error or 'unknown error'}."
        detail = self._human_detail(tool_result.data)
        if detail:
            return f"Completed: {user_intent}. Result: {detail}"
        return f"Completed: {user_intent}."

    def _human_detail(self, data: Any) -> str:
        if data is None:
            return ""
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, (int, float, bool)):
            return str(data)
        if isinstance(data, dict):
            values = [
                str(value).strip()
                for value in data.values()
                if isinstance(value, (str, int, float, bool)) and str(value).strip()
            ]
            return "；".join(values[:4])
        if isinstance(data, list):
            values = [
                str(value).strip()
                for value in data
                if isinstance(value, (str, int, float, bool)) and str(value).strip()
            ]
            return "；".join(values[:4])
        return str(data)
