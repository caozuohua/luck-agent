"""
core/model_router.py — 多模型路由（google-genai 版）
gemini-3.5-flash / 3.1-flash-lite（地区为空）+ 2.5 系列（us-central1）自动选择 + 故障切换
支持工具调用、对话历史注入、多区域客户端。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from core.log import get_logger
from google import genai
from google.genai import types

log = get_logger()

# 基础系统 Prompt（只保留不变的部分，任务专用 hint 由 IntentRouter 注入）
BASE_PROMPT = """你是部署在 GCP VPS 上的技术助手，通过 Lark 为用户提供服务。

## 环境说明
- VPS 路径（本地文件）：/opt/luck-agent、/opt/workspace 等真实系统路径
- GitHub 仓库路径：repo 内的相对路径（如 content/posts/xxx.md），通过 API 操作，与 VPS 无关
- **两者完全独立，绝对不要混淆**

## 行为准则
- 直接调用工具，不要描述步骤
- 每次只做用户要求的事，完成后简洁汇报
- 破坏性操作（删除/强推）前先告知用户
- 工具调用失败时，报告具体错误，不要重试超过 2 次
- 遇到权限问题时，先尝试只读检查、无交互 sudo（sudo -n）、systemd/service 或最小权限方案
- VPS 本地文件和 GitHub 仓库文件严格分离，不要把仓库相对路径当成 VPS 路径
- 博客写入优先通过 GitHub 仓库流程；查看博客用 list_blog_posts/get_blog_post，修改仓库文件用 get_file/update_file

{task_hint}

## 用户信息
{user_profile}

## 已验证可行的操作
{success_patterns}
"""


class ModelRouter:
    """多模型路由器，支持工具调用和故障切换。"""

    FALLBACK_CHAIN = {
        "gemini-3.5-flash":      ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "gemini-3.1-flash-lite": ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "gemini-2.5-pro":        ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "gemini-2.5-flash":      ["gemini-2.5-flash-lite"],
        "gemini-2.5-flash-lite": [],
    }

    # 模型 → 区域映射（不在映射中的使用默认区域）
    MODEL_REGION = {
        "gemini-3.5-flash":      "",
        "gemini-3.1-flash-lite": "",
    }

    def __init__(self, project: str, location: str) -> None:
        self._project = project
        self._default_location = location
        self._clients: dict[str, genai.Client] = {}
        self._ensure_client(location)
        self._tools_cache: dict[str, types.Tool] = {}
        self._temperature = 0.2
        self._max_tokens = int(os.environ.get("MAX_OUTPUT_TOKENS", "2048"))
        log.info("model_router_ready", project=project, location=location)

    def _ensure_client(self, location: str) -> genai.Client:
        if location not in self._clients:
            self._clients[location] = genai.Client(
                vertexai=True, project=self._project, location=location,
            )
        return self._clients[location]

    def _get_config(self, system: str = "") -> types.GenerateContentConfig:
        """生成配置（系统提示通过 config.system_instruction 字符串传递）"""
        config = types.GenerateContentConfig(
            temperature=self._temperature,
            max_output_tokens=self._max_tokens,
        )
        if system:
            config.system_instruction = system
        return config

    async def chat(
        self,
        model_name: str,
        messages: list[dict],
        tools_schema: list[dict] | None = None,
        system: str = "",
        user_id: str = "",
    ) -> dict:
        """
        发送消息，返回 {"text": str, "tool_calls": list, "model": str, "tokens": int}
        自动故障切换。
        """
        tools = self._build_tools(tools_schema) if tools_schema else None
        contents = self._build_contents(messages)

        models_to_try = [model_name] + self.FALLBACK_CHAIN.get(model_name, [])

        for model in models_to_try:
            try:
                result = await self._call(model, contents, tools, system)
                log.info("model_called", model=model, user_id=user_id[:8] if user_id else "")
                return result
            except Exception as e:
                log.warning("model_failed", model=model, error=str(e))
                if model == models_to_try[-1]:
                    raise
                await asyncio.sleep(0.5)

        raise RuntimeError("All models failed")

    async def _call(
        self,
        model_name: str,
        contents: list[types.Content],
        tools: types.Tool | None,
        system: str = "",
    ) -> dict:
        location = self.MODEL_REGION.get(model_name, self._default_location)
        client = self._ensure_client(location)
        config = self._get_config(system)
        if tools:
            config.tools = [tools]

        loop = asyncio.get_running_loop()

        def _sync_call():
            return client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )

        resp = await loop.run_in_executor(None, _sync_call)
        return self._parse_response(resp, model_name)

    def _parse_response(self, resp, model_name: str) -> dict:
        text_parts: list[str] = []
        tool_calls: list[dict] = []

        if not resp.candidates:
            return {
                "text": "",
                "tool_calls": [],
                "model": model_name,
                "tokens": 0,
            }

        for candidate in resp.candidates:
            content = candidate.content
            if not content or not content.parts:
                continue

            for part in content.parts:
                # 检查函数调用
                function_call = getattr(part, "function_call", None)
                if function_call and hasattr(function_call, "name") and function_call.name:
                    tool_calls.append({
                        "name": function_call.name,
                        "args": dict(function_call.args),
                    })
                else:
                    # 检查文本内容
                    text = getattr(part, "text", None)
                    if text:
                        text_parts.append(text)

        tokens = 0
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            tokens = resp.usage_metadata.total_token_count or 0

        return {
            "text": "".join(text_parts).strip(),
            "tool_calls": tool_calls,
            "model": model_name,
            "tokens": tokens,
        }

    def _build_tools(self, schemas: list[dict]) -> types.Tool | None:
        """构建工具"""
        if not schemas:
            return None

        cache_key = str(len(schemas))
        if cache_key in self._tools_cache:
            return self._tools_cache[cache_key]

        function_declarations = []
        for s in schemas:
            func = types.FunctionDeclaration(
                name=s["name"],
                description=s.get("description", ""),
                parameters=s.get("parameters", {}),
            )
            function_declarations.append(func)

        tool = types.Tool(function_declarations=function_declarations)
        self._tools_cache[cache_key] = tool
        return tool

    def _build_contents(self, messages: list[dict]) -> list[types.Content]:
        """构建消息内容（系统提示通过 system_instruction 参数单独传递）"""
        contents = []

        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            content = m["content"]

            # 处理函数调用结果
            if m["role"] == "tool":
                for tool_result in m.get("tool_results", []):
                    parts = [types.Part.from_function_response(
                        name=tool_result["name"],
                        response={"result": tool_result["result"]},
                    )]
                    contents.append(types.Content(role="user", parts=parts))
            else:
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=content)],
                ))

        return contents

    def build_system_prompt(self, user_profile: dict, history: list[dict],
                            success_patterns: list[dict] | None = None,
                            task_hint: str = "") -> str:
        """构建系统 Prompt，注入任务专用 hint（由 IntentRouter 提供）。"""
        profile_str = "\n".join(
            f"- {k}: {v}" for k, v in user_profile.items()
            if k not in ("default_chat_id", "default_git_dir")
        ) or "无"

        if success_patterns:
            pattern_lines = []
            for p in success_patterns[:8]:   # 最多注入 8 条，节省 token
                pattern_lines.append(
                    f"- [{p['tool']}] {p['intent'][:40]} → `{p['command'][:60]}`"
                    + (f"（×{p['use_count']}）" if p["use_count"] > 1 else "")
                )
            patterns_str = "\n".join(pattern_lines)
        else:
            patterns_str = "暂无"

        return BASE_PROMPT.format(
            task_hint=task_hint.strip(),
            user_profile=profile_str,
            success_patterns=patterns_str,
        )
