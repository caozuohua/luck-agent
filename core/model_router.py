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
SYSTEM_PROMPT = """你是一个部署在 GCP VPS 上的技术智能体，通过 Lark 为用户提供服务。

## 核心行为准则（最高优先级）
1. **默认探索，不默认拒绝**：遇到不确定能否完成的任务，先用工具探索当前状态，再决定下一步。不要在没有尝试工具之前说"无法完成"。
2. **先看再说**：不知道目录结构先 `ls`，不知道 git 状态先 `git status`，不知道命令是否存在先 `which`。用事实而非推断回答。
3. **工具优先**：能用工具完成的事绝不只描述步骤。调用工具 → 看结果 → 汇报。
4. **破坏性操作先确认**：删除、强制推送、覆盖等操作必须先告知用户再执行。
5. **回复简洁**：结果用代码块，进度用卡片，不重复已知信息。

## 工具能力边界
- `run_shell`：可执行 VPS 上任意 bash 命令，包括 git、pip、systemctl、文件操作等。**凡是系统级操作，直接用它。**
- `create_blog_post`：当用户说"发文章/写博客/更新博客"时调用，内容不完整时先创建草稿。
- `trigger_workflow`：当用户说"部署/发布/跑 CI"时调用。
- GitHub 系列工具：直接操作远端仓库，无需本地 git 环境。

## 已验证可行的操作
{success_patterns}

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
                            success_patterns: list[dict] | None = None) -> str:
        """构建含用户画像、成功模式和近期上下文的系统 prompt。"""
        profile_str = "\n".join(
            f"- {k}: {v}" for k, v in user_profile.items()
            if k != "default_chat_id"   # 过滤内部字段
        ) or "无特殊偏好"

        # 成功模式：按工具分组，最多注入 12 条
        if success_patterns:
            pattern_lines = []
            for p in success_patterns:
                pattern_lines.append(
                    f"- [{p['tool']}] {p['intent']} → `{p['command']}` ✅ {p['outcome']}"
                    + (f"（已用{p['use_count']}次）" if p["use_count"] > 1 else "")
                )
            patterns_str = "\n".join(pattern_lines)
        else:
            patterns_str = "暂无记录，请大胆尝试工具调用积累经验。"

        context_str = ""
        for h in history[-6:]:
            role = "用户" if h["role"] == "user" else "助手"
            snippet = h["content"][:100] + "…" if len(h["content"]) > 100 else h["content"]
            context_str += f"{role}: {snippet}\n"

        return SYSTEM_PROMPT.format(
            success_patterns=patterns_str,
            user_profile=profile_str,
            recent_context=context_str or "无近期上下文",
        )
