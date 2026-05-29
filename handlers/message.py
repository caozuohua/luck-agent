"""
handlers/message.py — AI 消息处理器（工具调用闭环）
ReAct 风格：模型 → 工具调用 → 结果注入 → 模型再推理，最多 N 轮。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from core.log import get_logger
from core.intent_router import route as intent_route, Intent

log = get_logger()

if TYPE_CHECKING:
    from core.memory import Memory, Message
    from core.model_router import ModelRouter
    from core.task_queue import TaskQueue
    from tools.github_tools import GitHubClient, GITHUB_TOOL_SCHEMAS
    from tools.shell_tools import ShellExecutor, FileManager, SHELL_TOOL_SCHEMAS
    from cards.builder import CardBuilder
    from config import Config

log = structlog.get_logger()

MAX_TOOL_ROUNDS = 6   # 防止无限工具循环


class AgentMessageHandler:
    """
    完整的 AI 消息处理闭环：
    1. 从记忆构建上下文
    2. 选择模型
    3. 调用模型（支持工具调用）
    4. 执行工具 → 结果注入 → 继续推理
    5. 保存对话到记忆
    6. 发送卡片回复
    """

    def __init__(
        self,
        config,
        memory: "Memory",
        router: "ModelRouter",
        queue: "TaskQueue",
        github: "GitHubClient",
        shell: "ShellExecutor",
        file_mgr: "FileManager",
        card: type["CardBuilder"],
        lark_reply_fn,
    ) -> None:
        self.cfg      = config
        self.memory   = memory
        self.router   = router
        self.queue    = queue
        self.github   = github
        self.shell    = shell
        self.file_mgr = file_mgr
        self.card     = card
        self.reply    = lark_reply_fn

        # 合并所有工具 schema
        from tools.github_tools import GITHUB_TOOL_SCHEMAS
        from tools.shell_tools  import SHELL_TOOL_SCHEMAS
        self.all_tools = GITHUB_TOOL_SCHEMAS + SHELL_TOOL_SCHEMAS + [
            {
                "name": "remember",
                "description": "保存用户的偏好、习惯、重要信息到持久化记忆。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key":   {"type": "string", "description": "记忆键，如 preferred_language"},
                        "value": {"type": "string", "description": "记忆值"},
                    },
                    "required": ["key", "value"],
                },
            },
            {
                "name": "recall",
                "description": "查询之前保存的用户信息或偏好。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "要查询的记忆键"},
                    },
                    "required": ["key"],
                },
            },
        ]

    async def handle(
        self,
        user_id: str,
        chat_id: str,
        message_id: str,
        text: str,
        model_override: str = "",   # 非空时强制使用指定模型
    ) -> None:
        t0 = time.monotonic()

        # 1. 意图路由（零 AI，纯规则）→ 确定工具子集和任务 hint
        route = intent_route(text)
        log.info("intent_routed",
                 intent=route.intent.value,
                 confidence=round(route.confidence, 2),
                 tools=len(route.tool_names),
                 user_id=user_id[:8])

        # 2. 选择模型（override > 路由建议 > 自动）
        if not model_override:
            if route.model_hint == "pro":
                model = self.cfg.MODEL_PRO
            elif route.model_hint == "flash":
                model = self.cfg.MODEL_FLASH
            else:
                model = self.cfg.pick_model(text)
        else:
            model = model_override

        # 3. 构建系统 Prompt（注入任务 hint，减少 token）
        profile  = self.memory.get_all_profile(user_id)
        patterns = self.memory.get_success_patterns(limit=8)
        history  = self.memory.get_history(user_id, limit=10)  # 减少历史条数
        system   = self.router.build_system_prompt(
            profile, history, patterns,
            task_hint=route.prompt_hint,
        )

        # 4. 按意图选择工具子集（GENERAL = 全量）
        if route.tool_names:
            active_tools = [t for t in self.all_tools
                            if t["name"] in route.tool_names]
        else:
            active_tools = self.all_tools

        # 5. 拼接消息（只带最近 6 条历史，减少混淆）
        recent_history = history[-6:] if len(history) > 6 else history
        messages = recent_history + [{"role": "user", "content": text}]

        # 6. 工具调用循环
        final_text   = ""
        tool_rounds  = 0
        all_tool_results: list[dict] = []

        while tool_rounds < MAX_TOOL_ROUNDS:
            try:
                result = await self.router.chat(
                    model_name=model,
                    messages=messages,
                    tools_schema=active_tools,   # ← 最小工具子集
                    system=system,
                    user_id=user_id,
                )
            except Exception as e:
                log.error("model_call_failed", error=str(e), user_id=user_id[:8])
                await self.reply(chat_id, card=self.card.error(
                    "模型调用失败",
                    f"{type(e).__name__}: {e}\n\n💡 提示：直接指令（/sh、/deploy 等）仍可使用。"
                ))
                return

            # 有工具调用 → 执行 → 结果注入
            if result["tool_calls"]:
                tool_rounds += 1
                tool_results = []

                for tc in result["tool_calls"]:
                    tool_name = tc["name"]
                    tool_args = tc["args"]
                    log.info("tool_call", tool=tool_name, args=str(tool_args)[:100],
                             user_id=user_id[:8])

                    try:
                        tool_output = await self._dispatch_tool(
                            tool_name, tool_args, user_id, chat_id
                        )
                    except Exception as e:
                        tool_output = {"error": str(e)}

                    tool_results.append({"tool": tool_name, "result": tool_output})
                    all_tool_results.append({"tool": tool_name, "result": tool_output})

                # 工具结果注入：assistant 思考 + user 反馈，让模型继续推理
                if result["text"]:
                    messages.append({"role": "assistant", "content": result["text"]})
                messages.append({
                    "role": "user",
                    "content": (
                        "工具执行完毕，结果如下，请根据结果给用户一个完整的中文总结回复：\n"
                        + json.dumps(tool_results, ensure_ascii=False, indent=2)
                    ),
                })

            else:
                # 无工具调用 → 最终回复
                final_text = result["text"]
                elapsed    = time.monotonic() - t0

                # final_text 为空时，用工具结果自动生成兜底摘要
                if not final_text and all_tool_results:
                    final_text = self._summarize_tool_results(all_tool_results)

                # 仍然为空（无工具调用且模型无输出）→ 通用提示
                if not final_text:
                    final_text = "✅ 操作已完成。"

                # 4. 保存到记忆
                from core.memory import Message as Msg
                self.memory.add_message(Msg(user_id, "user", text))
                self.memory.add_message(Msg(user_id, "assistant", final_text,
                                           model=result["model"], tokens=result["tokens"]))

                # 5. 发送回复卡片
                await self.reply(
                    chat_id,
                    card=self.card.agent_reply(
                        text=final_text,
                        model=result["model"],
                        elapsed=elapsed,
                    ),
                )

                log.info("message_handled",
                         user_id=user_id[:8],
                         model=result["model"],
                         tool_rounds=tool_rounds,
                         elapsed=round(elapsed, 2))
                return

        # 超过工具调用轮数上限
        await self.reply(chat_id, card=self.card.error(
            "工具调用轮数超限",
            f"已执行 {MAX_TOOL_ROUNDS} 轮工具调用但未得到最终答案，请尝试简化问题。"
        ))

    # ── 工具分发 ──────────────────────────────────────────────────────
    async def _dispatch_tool(
        self, name: str, args: dict, user_id: str, chat_id: str
    ) -> Any:
        """将模型的工具调用路由到对应实现。"""

        # ── GitHub 工具 ──
        if name == "create_blog_post":
            result = await self.github.create_blog_post(**args)
            self.memory.log_github(user_id, args.get("repo",""), "create_blog_post",
                                   args.get("title",""), str(result))
            # 成功后顺便触发 deploy
            repo = args.get("repo", self.cfg.HUGO_REPO)
            try:
                await self.github.trigger_workflow(repo, "deploy.yml")
                result["deploy_triggered"] = True
            except Exception:
                result["deploy_triggered"] = False
            return result

        elif name == "list_blog_posts":
            return await self.github.list_blog_posts(**args)

        elif name == "trigger_workflow":
            result = await self.github.trigger_workflow(**args)
            self.memory.log_github(user_id, args.get("repo",""), "trigger_workflow",
                                   str(args), str(result))
            return result

        elif name == "list_workflow_runs":
            runs = await self.github.list_workflow_runs(**args)
            return runs

        elif name == "list_issues":
            return await self.github.list_issues(**args)

        elif name == "create_issue":
            return await self.github.create_issue(**args)

        elif name == "list_prs":
            return await self.github.list_prs(**args)

        elif name == "get_file":
            return await self.github.get_file(**args)

        elif name == "update_file":
            return await self.github.update_file(**args)

        elif name == "list_commits":
            return await self.github.list_commits(**args)

        elif name == "get_repo_info":
            return await self.github.get_repo_info(**args)

        # ── Shell 工具 ──
        elif name == "run_shell":
            cmd = args.get("command", "")
            if self.shell.is_dangerous(cmd):
                return {"error": "危险命令已被拦截，请使用 /sh! 强制执行。"}
            result = await self.shell.run(
                cmd,
                cwd=args.get("cwd"),
                timeout=args.get("timeout"),
            )
            return result

        elif name == "run_script":
            result = await self.shell.run_script(args.get("script", ""))
            return result

        elif name == "list_files":
            return self.file_mgr.list_dir(args.get("path", ""))

        elif name == "read_file":
            return self.file_mgr.read_file(args.get("path", ""))

        elif name == "write_file":
            return self.file_mgr.write_file(args.get("path",""), args.get("content",""))

        elif name == "disk_usage":
            return self.file_mgr.disk_usage()

        # ── 记忆工具 ──
        elif name == "remember":
            self.memory.set_profile(user_id, args["key"], args["value"])
            return {"saved": True, "key": args["key"]}

        elif name == "recall":
            val = self.memory.get_profile(user_id, args["key"])
            return {"key": args["key"], "value": val}

        else:
            return {"error": f"未知工具：{name}"}

    # ── 兜底摘要 ──────────────────────────────────────────────────────
    def _summarize_tool_results(self, tool_results: list[dict]) -> str:
        """
        模型没有生成总结文字时，从工具结果自动拼一条简明摘要。
        只提取关键字段，避免把整个 JSON 堆给用户。
        """
        lines = []
        for tr in tool_results:
            tool = tr["tool"]
            res  = tr["result"]
            if isinstance(res, dict) and res.get("error"):
                lines.append(f"❌ `{tool}` 失败：{res['error']}")
                continue

            if tool == "create_blog_post":
                action = "更新" if res.get("action") == "update" else "创建"
                lines.append(
                    f"✅ 博文已{action}：`{res.get('path','')}`\n"
                    f"   commit `{res.get('commit','')}` · "
                    f"{'🚀 已触发部署' if res.get('deploy_triggered') else '⚠️ 部署未触发'}"
                )
            elif tool == "trigger_workflow":
                lines.append(f"🚀 已触发 workflow：`{res.get('workflow','')}` @ `{res.get('ref','')}`")
            elif tool == "run_shell":
                rc = res.get("returncode", -1)
                icon = "✅" if rc == 0 else "❌"
                out = (res.get("stdout") or "").strip()[:200]
                lines.append(f"{icon} Shell 返回码 {rc}" + (f"\n```\n{out}\n```" if out else ""))
            elif tool == "create_issue":
                lines.append(f"✅ Issue #{res.get('number','')} 已创建：{res.get('url','')}")
            elif tool == "update_file":
                lines.append(f"✅ 文件已更新：`{res.get('path','')}` commit `{res.get('commit','')}`")
            elif tool == "merge_pr":
                lines.append(f"✅ PR 已合并，commit `{res.get('sha','')}`")
            elif tool in ("remember", "recall"):
                pass   # 记忆操作静默处理
            else:
                # 通用：只取第一个非空字段值
                preview = next(
                    (str(v)[:100] for v in res.values() if v and not isinstance(v, (dict, list))),
                    "完成"
                ) if isinstance(res, dict) else str(res)[:100]
                lines.append(f"✅ `{tool}`：{preview}")

        return "\n".join(lines) if lines else "✅ 操作已完成。"
