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
        self.scheduler = None   # 由 agent.py 启动后注入

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
            {
                "name": "forget",
                "description": (
                    "删除用户画像中的某个记忆条目。"
                    "当用户说'忘掉我的XXX'、'删除你记的XXX'、'不要记住XXX'时调用。"
                    "key='*' 表示清空全部用户画像。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "要删除的记忆键，或 '*' 清空全部",
                        },
                    },
                    "required": ["key"],
                },
            },
            {
                "name": "show_memory",
                "description": (
                    "展示智能体当前记忆的完整内容，包括用户画像、成功模式、对话历史摘要。"
                    "当用户问'你记得什么'、'你的记忆里有什么'、'看看你的记忆'时调用。"
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "schedule_task",
                "description": (
                    "设置定时任务，让智能体在指定时间自动执行并推送结果。"
                    "当用户说'每天早上提醒我'、'每周一检查'、'每隔X小时执行'时调用。"
                    "mode=cron 时 schedule 为标准 5 字段 cron 表达式（分 时 日 月 周）；"
                    "mode=interval 时 schedule 为间隔秒数字符串，如 '3600' 表示每小时。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":     {"type": "string", "description": "任务名称，如'每日博客检查'"},
                        "prompt":   {"type": "string", "description": "到时间后智能体要执行的指令，用自然语言描述"},
                        "mode":     {"type": "string", "enum": ["cron", "interval"]},
                        "schedule": {"type": "string",
                                     "description": "cron: '0 9 * * 1-5'（周一至周五9点）；interval: '3600'（每小时）"},
                    },
                    "required": ["name", "prompt", "mode", "schedule"],
                },
            },
            {
                "name": "list_schedules",
                "description": "查看当前用户的所有定时任务列表。",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "cancel_schedule",
                "description": "取消（删除）一个定时任务。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "任务ID，从 list_schedules 获取"},
                    },
                    "required": ["task_id"],
                },
            },
            {
                "name": "pause_schedule",
                "description": "暂停一个定时任务（不删除，可恢复）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                    },
                    "required": ["task_id"],
                },
            },
            {
                "name": "resume_schedule",
                "description": "恢复一个已暂停的定时任务。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                    },
                    "required": ["task_id"],
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

        # 1. 选择模型（override 优先）
        model = model_override or self.cfg.pick_model(text)

        # 2. 构建对话历史
        history  = self.memory.get_history(user_id, limit=self.cfg.MEMORY_MAX_CONTEXT)
        profile  = self.memory.get_all_profile(user_id)
        patterns = self.memory.get_success_patterns(limit=12)
        system   = self.router.build_system_prompt(profile, history, patterns)

        # 拼接消息（历史 + 当前）
        messages = history + [{"role": "user", "content": text}]

        # 3. 工具调用循环
        final_text   = ""
        tool_rounds  = 0
        all_tool_results: list[dict] = []   # 累积所有工具结果，用于兜底摘要

        while tool_rounds < MAX_TOOL_ROUNDS:
            try:
                result = await self.router.chat(
                    model_name=model,
                    messages=messages,
                    tools_schema=self.all_tools,
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
                        # 工具成功 → 记录模式（失败不记录）
                        if not (isinstance(tool_output, dict)
                                and tool_output.get("error")):
                            self._record_pattern(tool_name, tool_args,
                                                 tool_output, text)
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

        elif name == "forget":
            key = args.get("key", "")
            if key == "*":
                count = self.memory.clear_profile(user_id)
                return {"deleted": True, "key": "*", "count": count}
            ok = self.memory.delete_profile(user_id, key)
            return {"deleted": ok, "key": key}

        elif name == "show_memory":
            profile  = self.memory.get_all_profile(user_id)
            patterns = self.memory.get_success_patterns(limit=20)
            history  = self.memory.get_history(user_id, limit=5)
            return {
                "profile":  profile,
                "patterns": [
                    {"id": p["id"], "tool": p["tool"], "intent": p["intent"],
                     "use_count": p["use_count"]}
                    for p in patterns
                ],
                "recent_messages": len(history),
                "history_preview": [
                    {"role": h["role"], "snippet": h["content"][:80]}
                    for h in history
                ],
            }

        # ── 定时任务工具 ──
        elif name == "schedule_task":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            from core.scheduler import next_cron_desc, _cron_matches
            mode     = args.get("mode", "cron")
            schedule = args.get("schedule", "")
            task_name = args.get("name", "定时任务")
            prompt   = args.get("prompt", "")

            # 验证 cron 格式
            if mode == "cron":
                test_dt = datetime.now(timezone.utc)
                try:
                    _cron_matches(schedule, test_dt)
                except Exception:
                    return {"error": f"cron 表达式格式错误：{schedule}，示例：'0 9 * * 1-5'"}
                task = self.scheduler.add_cron(
                    user_id, chat_id, task_name, prompt, schedule
                )
                desc = next_cron_desc(schedule)
            else:
                try:
                    seconds = int(schedule)
                    if seconds < 60:
                        return {"error": "interval 最小 60 秒"}
                except ValueError:
                    return {"error": f"interval 应为秒数，如 '3600'，收到：{schedule}"}
                task = self.scheduler.add_interval(
                    user_id, chat_id, task_name, prompt, seconds
                )
                h, m = divmod(seconds // 60, 60)
                desc = f"每{h}小时{m}分钟" if h else f"每{m}分钟"

            return {
                "task_id":  task.id,
                "name":     task.name,
                "mode":     mode,
                "schedule": desc,
                "status":   "已创建",
            }

        elif name == "list_schedules":
            if not self.scheduler:
                return {"schedules": []}
            from core.scheduler import next_cron_desc
            tasks = self.scheduler.list_user(user_id)
            return {
                "schedules": [
                    {
                        "id":       t.id,
                        "name":     t.name,
                        "mode":     t.mode,
                        "schedule": next_cron_desc(t.schedule) if t.mode == "cron"
                                    else f"每{int(t.schedule)//60}分钟",
                        "enabled":  t.enabled,
                        "run_count": t.run_count,
                        "prompt":   t.prompt[:60] + "…" if len(t.prompt) > 60 else t.prompt,
                    }
                    for t in tasks
                ]
            }

        elif name == "cancel_schedule":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            ok = self.scheduler.cancel(args.get("task_id", ""))
            return {"cancelled": ok}

        elif name == "pause_schedule":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            ok = self.scheduler.pause(args.get("task_id", ""))
            return {"paused": ok}

        elif name == "resume_schedule":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            ok = self.scheduler.resume(args.get("task_id", ""))
            return {"resumed": ok}

        else:
            return {"error": f"未知工具：{name}"}

    # ── 成功模式记录 ─────────────────────────────────────────────────
    def _record_pattern(self, tool: str, args: dict,
                        output: Any, user_text: str) -> None:
        """从工具调用结果提取关键信息，写入 success_patterns。"""
        try:
            # intent：取用户原始输入前 50 字作为意图描述
            intent = user_text[:50].replace("\n", " ")

            # command：从 args 提取最有代表性的参数
            if tool == "run_shell":
                command = args.get("command", "")[:80]
            elif tool == "create_blog_post":
                command = f"repo={args.get('repo','')} title={args.get('title','')[:30]}"
            elif tool == "trigger_workflow":
                command = f"repo={args.get('repo','')} wf={args.get('workflow_id','')}"
            elif tool in ("get_file", "update_file", "read_file", "write_file"):
                command = f"path={args.get('path', '')}"
            else:
                command = str(args)[:80]

            # outcome：从输出提取结果摘要
            if isinstance(output, dict):
                if tool == "run_shell":
                    rc = output.get("returncode", 0)
                    out_preview = (output.get("stdout") or "").strip()[:60]
                    outcome = f"rc={rc} {out_preview}"
                elif tool == "create_blog_post":
                    outcome = (f"{output.get('action','done')} "
                               f"commit={output.get('commit','')} "
                               f"deploy={output.get('deploy_triggered','?')}")
                else:
                    # 取第一个有意义的字符串值
                    outcome = next(
                        (str(v)[:60] for v in output.values()
                         if v and isinstance(v, (str, int, bool))),
                        "success"
                    )
            else:
                outcome = str(output)[:60]

            self.memory.record_success(tool, intent, command, outcome)
        except Exception as e:
            log.debug("pattern_record_failed", error=str(e))

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
            elif tool == "forget":
                key = res.get("key", "")
                if key == "*":
                    lines.append(f"✅ 已清空全部用户画像（{res.get('count', 0)} 条）。")
                elif res.get("deleted"):
                    lines.append(f"✅ 已删除记忆条目：`{key}`")
                else:
                    lines.append(f"❌ 找不到记忆条目：`{key}`")

            elif tool == "show_memory":
                profile  = res.get("profile", {})
                patterns = res.get("patterns", [])
                msgs     = res.get("recent_messages", 0)
                lines.append(
                    f"📋 当前记忆：画像 {len(profile)} 条 · "
                    f"成功模式 {len(patterns)} 条 · 近期对话 {msgs} 条"
                )
                if profile:
                    INTERNAL = {"default_chat_id", "default_git_dir"}
                    for k, v in profile.items():
                        if k not in INTERNAL:
                            lines.append(f"  - `{k}`: {v}")

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
