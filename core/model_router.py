"""
core/model_router.py — 多模型路由
gemini-2.5-pro / flash / flash-lite 自动选择 + 故障切换
支持工具调用、流式响应、对话历史注入。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from core.log import get_logger
import vertexai
from vertexai.generative_models import (
    Content,
    FunctionDeclaration,
    GenerationConfig,
    GenerativeModel,
    Part,
    Tool,
)

log = get_logger()

# 系统 Prompt
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

{task_hint}

## 用户信息
{user_profile}

## 已验证可行的操作
{success_patterns}
"""


class ModelRouter:
    """多模型路由器，支持工具调用和故障切换。"""

    FALLBACK_CHAIN = {
        "gemini-2.5-pro":       ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "gemini-2.5-flash":     ["gemini-2.5-flash-lite"],
        "gemini-2.5-flash-lite": [],
    }

    def __init__(self, project: str, location: str) -> None:
        vertexai.init(project=project, location=location)
        # 只缓存 Tool 对象（schema 不变，构建有一定开销）
        # GenerativeModel 本身不缓存：它很轻，且 system_prompt 每次对话都不同
        self._tools_cache: dict[str, list[Tool]] = {}
        self._gen_config = GenerationConfig(
            temperature=0.2,
            # flash-lite/flash 用 2048 够用；pro 场景需要长输出时模型会自动延伸
            max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", "2048")),
        )
        log.info("model_router_ready", project=project)

    def _get_model(self, model_name: str, tools: list[Tool] | None = None,
                   system: str = "") -> GenerativeModel:
        """每次构造新实例，确保 system_prompt 是最新的。实例本身很轻。"""
        return GenerativeModel(
            model_name,
            tools=tools or None,
            system_instruction=system or None,
            generation_config=self._gen_config,
        )

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
        # Tool 对象缓存（schema 固定，避免重复构建）
        if tools_schema:
            cache_key = str(len(tools_schema))  # schema 条数作为 key，足够区分
            if cache_key not in self._tools_cache:
                self._tools_cache[cache_key] = self._build_tools(tools_schema)
            tools = self._tools_cache[cache_key]
        else:
            tools = None

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

    async def _call(self, model_name: str, contents: list[Content],
                    tools: list[Tool] | None, system: str) -> dict:
        model = self._get_model(model_name, tools, system)
        loop  = asyncio.get_running_loop()

        def _sync_call():
            resp = model.generate_content(contents)
            return resp

        resp = await loop.run_in_executor(None, _sync_call)
        return self._parse_response(resp, model_name)

    def _parse_response(self, resp, model_name: str) -> dict:
        text_parts: list[str] = []
        tool_calls: list[dict] = []

        for candidate in resp.candidates:
            for part in candidate.content.parts:
                # 先用 getattr 安全取 function_call，再检查 name 是否非空
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    tool_calls.append({
                        "name": fc.name,
                        "args": dict(fc.args),
                    })
                else:
                    text = getattr(part, "text", None)
                    if text:
                        text_parts.append(text)

        tokens = 0
        if hasattr(resp, "usage_metadata"):
            tokens = resp.usage_metadata.total_token_count

        return {
            "text":       "".join(text_parts).strip(),
            "tool_calls": tool_calls,
            "model":      model_name,
            "tokens":     tokens,
        }

    def _build_tools(self, schemas: list[dict]) -> list[Tool]:
        decls = []
        for s in schemas:
            decls.append(FunctionDeclaration(
                name=s["name"],
                description=s["description"],
                parameters=s.get("parameters", {}),
            ))
        return [Tool(function_declarations=decls)]

    def _build_contents(self, messages: list[dict]) -> list[Content]:
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append(Content(
                role=role,
                parts=[Part.from_text(m["content"])],
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
