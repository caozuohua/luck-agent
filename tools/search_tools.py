"""多后端免费搜索工具，支持轮询故障切换"""
from __future__ import annotations
import asyncio
import httpx
import os
from core.log import get_logger

log = get_logger()


class SearchTools:
    """多后端搜索工具，支持 Tavily、DuckDuckGo、SearXNG、Qwant"""
    
    BACKENDS = [
        {"name": "tavily", "url": "https://egg-search-gamma.vercel.app/search", "key": os.getenv("TAVILY_API_KEY", "")},
        {"name": "duckduckgo", "url": "https://api.duckduckgo.com/"},
        {"name": "searxng", "url": "https://searx.be/api/search"},
        {"name": "qwant", "url": "https://api.qwant.com/v3/search/web"},
    ]
    
    def __init__(self):
        self._current_backend = 0
    
    async def search(self, query: str) -> dict:
        """执行搜索，自动轮询后端"""
        backends = self.BACKENDS.copy()
        for i in range(len(backends)):
            backend = backends[(self._current_backend + i) % len(backends)]
            try:
                result = await self._search_with_backend(backend, query)
                self._current_backend = (self._current_backend + 1) % len(backends)
                return result
            except Exception as e:
                log.warning("search_backend_failed", backend=backend["name"], error=str(e)[:200])
                await asyncio.sleep(0.5)
        
        return {"error": "所有搜索后端都不可用"}
    
    async def _search_with_backend(self, backend: dict, query: str) -> dict:
        name = backend["name"]
        url = backend["url"]
        key = backend.get("key", "")
        
        if name == "tavily":
            return await self._tavily_search(query, url, key)
        elif name == "duckduckgo":
            return await self._duckduckgo_search(query, url)
        elif name == "searxng":
            return await self._searxng_search(query, url)
        elif name == "qwant":
            return await self._qwant_search(query, url)
        
        raise ValueError(f"未知后端: {name}")
    
    async def _tavily_search(self, query: str, url: str, api_key: str) -> dict:
        """Tavily 搜索（通过 Vercel 代理）"""
        params = {"q": query}
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params, headers=headers or None)
            resp.raise_for_status()
            return self._format_tavily_result(resp.json())
    
    def _format_tavily_result(self, data: dict) -> dict:
        """格式化 Tavily API 返回结果（兼容官方格式和 Vercel 代理格式）"""
        result = {"results": [], "summary": ""}

        # 标准 Tavily 格式：results 数组 + answer 摘要
        items = data.get("results", [])
        for item in items[:5]:
            result["results"].append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("content", ""),
            })

        if data.get("answer"):
            result["summary"] = data["answer"]

        # Vercel 代理格式：所有结果拼在 result 字符串里，results 数组为空
        if not result["results"] and not result["summary"]:
            proxy_result = data.get("result", "")
            if proxy_result:
                result["summary"] = proxy_result

        return result
    
    async def _duckduckgo_search(self, query: str, url: str) -> dict:
        params = {"q": query, "format": "json", "no_html": "1", "no_redirect": "1"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return self._format_duckduckgo_result(resp.json())

    async def _searxng_search(self, query: str, url: str) -> dict:
        params = {"q": query, "format": "json", "language": "zh"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return self._format_searxng_result(resp.json())

    async def _qwant_search(self, query: str, url: str) -> dict:
        params = {"q": query, "t": "web", "locale": "zh"}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return self._format_qwant_result(resp.json())

    def _format_duckduckgo_result(self, data: dict) -> dict:
        """解析 DuckDuckGo Instant Answer API 返回。

        优先级：Answer（直接回答）> AbstractText（摘要）> RelatedTopics（相关条目）
        """
        result = {"results": [], "summary": "", "source": ""}

        # 1. 直接回答（计算器、定义等）
        answer = data.get("Answer", "").strip()
        if answer:
            result["summary"] = answer
            return result

        # 2. 摘要
        abstract = data.get("AbstractText", "").strip()
        if abstract:
            result["summary"] = abstract
            url = data.get("AbstractURL", "")
            if url:
                result["source"] = url
            # 摘要也算一条结果
            result["results"].append({
                "title": data.get("Heading", abstract[:60]),
                "url": url,
                "description": abstract,
            })
            return result

        # 3. 相关条目
        topics = data.get("RelatedTopics", [])
        for topic in topics[:5]:
            # RelatedTopics 可能是直接条目，也可能是子分类（含 "Topics" 键）
            if "Text" in topic and "FirstURL" in topic:
                result["results"].append({
                    "title": topic["Text"],
                    "url": topic["FirstURL"],
                })
            elif "Topics" in topic:
                for sub in topic["Topics"][:3]:
                    if "Text" in sub and "FirstURL" in sub:
                        result["results"].append({
                            "title": sub["Text"],
                            "url": sub["FirstURL"],
                        })

        return result
    
    def _format_searxng_result(self, data: dict) -> dict:
        result = {"results": [], "summary": ""}
        for item in data.get("results", [])[:5]:
            result["results"].append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("content", ""),
            })
        return result
    
    def _format_qwant_result(self, data: dict) -> dict:
        result = {"results": [], "summary": ""}
        items = data.get("data", {}).get("result", {}).get("items", [])
        for item in items[:5]:
            result["results"].append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("desc", ""),
            })
        return result
    
    def format_result(self, result: dict) -> str:
        if "error" in result:
            return f"❌ {result['error']}"
        
        output = []
        if result.get("summary"):
            output.append(f"📋 {result['summary']}")
            if result.get("source"):
                output.append(f"🔗 来源: {result['source']}")
        
        if result.get("results"):
            output.append("\n📌 搜索结果:")
            for i, item in enumerate(result["results"], 1):
                title = item.get("title", "")
                url = item.get("url", "")
                desc = item.get("description", "")
                line = f"{i}. [{title}]({url})"
                if desc:
                    line += f"\n   {desc}"
                output.append(line)
        
        return "\n".join(output) if output else "未找到相关结果"


# ── Tool Schema（供 ModelRouter 工具调用注册）─────────────────────────────────
SEARCH_TOOL_SCHEMAS = [
    {
        "name": "search_web",
        "description": "在互联网上搜索最新信息，优先使用 Tavily 搜索引擎（通过 Vercel），失败时自动 fallback 到 DuckDuckGo、SearXNG、Qwant。用于获取新闻、技术文档、最新资讯等。当你不知道答案或需要最新信息时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，用自然语言描述你想查找的内容"},
            },
            "required": ["query"],
        },
    },
]
