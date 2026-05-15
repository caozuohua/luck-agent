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

import structlog
import vertexai
from vertexai.generative_models import (
    Content,
    FunctionDeclaration,
    GenerationConfig,
    GenerativeModel,
    Part,
    Tool,
)

log = structlog.get_logger()

# 系统 Prompt
SYSTEM_PROMPT = """你是一个专业的技术助手，部署在 Google Cloud VPS 上，通过飞书（Lark）为用户提供服务。

## 核心行为准则
1. 你有 run_shell 工具，可以在 VPS 上执行任意 bash 命令。
2. 遇到任何不确定能否完成的任务，优先尝试用工具探索，而不是声明"无法完成"。
3. 遇到不确定的任务，第一步永远是用 run_shell 探索当前状态：
   - 不知道目录结构 → 先 ls
   - 不知道 git 状态 → 先 git status  
   - 不知道某个命令是否存在 → 先 which 或 --help
4. 探索之后再决定能不能做，而不是凭记忆判断。

## 行为准则
1. 优先使用工具完成任务，而非仅描述步骤
2. 执行破坏性操作（删除/强制推送）前必须确认
3. Shell 命令执行后汇报结果和副作用
4. 长任务提交后台队列，主动告知 task_id 和预计完成时间
5. 回复简洁，技术细节用代码块，进度用卡片展示

## 用户偏好
{user_profile}

## 近期上下文
{recent_context}
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
        self._model_cache: dict[str, GenerativeModel] = {}
        log.info("model_router_ready", project=project)

    def _get_model(self, model_name: str, tools: list[Tool] | None = None,
                   system: str = "") -> GenerativeModel:
        key = f"{model_name}:{bool(tools)}"
        if key not in self._model_cache:
            self._model_cache[key] = GenerativeModel(
                model_name,
                tools=tools,
                system_instruction=system or None,
                generation_config=GenerationConfig(
                    temperature=0.2,
                    max_output_tokens=4096,
                ),
            )
        return self._model_cache[key]

    async def chat(
        self,
        model_name: str,
        messages: list[dict],        # [{"role": "user/assistant", "content": "..."}]
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

    def build_system_prompt(self, user_profile: dict, history: list[dict]) -> str:
        """构建含用户画像和近期上下文的系统 prompt。"""
        profile_str = "\n".join(
            f"- {k}: {v}" for k, v in user_profile.items()
        ) or "无特殊偏好"

        context_str = ""
        for h in history[-6:]:  # 最近 6 条做摘要
            role = "用户" if h["role"] == "user" else "助手"
            context_str += f"{role}: {h['content'][:100]}...\n" if len(h["content"]) > 100 else f"{role}: {h['content']}\n"

        return SYSTEM_PROMPT.format(
            user_profile=profile_str,
            recent_context=context_str or "无近期上下文",
        )
