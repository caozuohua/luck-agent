from __future__ import annotations

import os
from typing import Any

import httpx

from tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web using Serper.dev. Parameters: query (string), "
        "num_results (integer, default 5). Returns data as a list of "
        "{title, url, snippet}."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "num_results": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }
    endpoint = "https://google.serper.dev/search"

    async def run(self, **kwargs: Any) -> ToolResult:
        return await self.search(
            str(kwargs.get("query", "")),
            int(kwargs.get("num_results", 5)),
        )

    async def search(self, query: str, num_results: int = 5) -> ToolResult:
        query = query.strip()
        if not query:
            return ToolResult.fail(error="query is required", tool_name=self.name)
        api_key = os.environ.get("SERPER_API_KEY", "").strip()
        if not api_key:
            return ToolResult.fail(error="SERPER_API_KEY is required", tool_name=self.name)
        num_results = max(1, min(int(num_results), 10))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "X-API-KEY": api_key,
                        "Content-Type": "application/json",
                    },
                    json={"q": query, "num": num_results},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return ToolResult.fail(error=str(exc), tool_name=self.name)
        results = [
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("link") or item.get("url") or ""),
                "snippet": str(item.get("snippet", "")),
            }
            for item in payload.get("organic", [])[:num_results]
        ]
        return ToolResult.ok(data=results, tool_name=self.name)

    def docstring(self, task_context: str = "", experience: str = "") -> str:
        base = super().docstring(task_context=task_context, experience=experience)
        return (
            base
            + "\nL1: Parameters: query, num_results. Return format: [{title, url, snippet}]."
            + "\nL2: 当任务需要实时信息、最新消息、新闻、搜索结果或 external knowledge 时使用。"
        )
