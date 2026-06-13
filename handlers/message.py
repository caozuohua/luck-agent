"""
handlers/message.py — AI 消息处理器（工具调用闭环）
ReAct 风格：模型 → 工具调用 → 结果注入 → 模型再推理，最多 N 轮。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from core.log import get_logger
from core.intent_router import route as intent_route, Intent
from core.topics import normalize_topic, normalize_topics
from tools.pkb_tools import PkbClient, VALID_PKB_TYPES, get_pkb_client

log = get_logger()

if TYPE_CHECKING:
    from core.memory import Memory, Message
    from core.model_router import ModelRouter
    from core.task_queue import TaskQueue
    from tools.github_tools import GitHubClient, GITHUB_TOOL_SCHEMAS
    from tools.shell_tools import ShellExecutor, FileManager, SHELL_TOOL_SCHEMAS
    from cards.builder import CardBuilder
    from config import Config

MAX_TOOL_ROUNDS = 6   # 防止无限工具循环
VALID_NOTE_TYPES = VALID_PKB_TYPES

PKB_TOOL_SCHEMAS = [
    {
        "name": "pkb_save",
        "description": "保存长期有价值的知识。仅在用户明确要求记住、保存或加入知识库时使用；不得保存密码、令牌、私钥或未经确认的敏感个人信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "source": {"type": "string", "description": "默认 luck-agent"},
                "type": {"type": "string", "enum": ["fact", "idea", "task", "question", "code"]},
                "topics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["content"],
        },
    },
    {
        "name": "pkb_search",
        "description": "检索知识库。通常不要传 source，只有用户明确要求限定来源时才传。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "source": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "pkb_get",
        "description": "按 ID 获取完整知识，适合搜索结果上下文不足时继续读取。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "pkb_list",
        "description": "浏览最近或指定类型、主题、时间范围的知识。",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
                "type": {"type": "string", "enum": ["fact", "idea", "task", "question", "code"]},
                "topics": {"type": "array", "items": {"type": "string"}},
                "from": {"type": "string"},
                "to": {"type": "string"},
                "include_deleted": {"type": "boolean"},
            },
        },
    },
    {
        "name": "pkb_update",
        "description": "修正已定位的知识，至少传 content、type、topics、summary 之一。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "content": {"type": "string"},
                "type": {"type": "string", "enum": ["fact", "idea", "task", "question", "code"]},
                "topics": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "pkb_delete",
        "description": "软删除已定位的知识。调用前必须获得用户确认；不得永久删除。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "pkb_restore",
        "description": "恢复已软删除的知识。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]


def parse_note_message(text: str) -> tuple[str, str, list[str]] | None:
    """解析以 # 开头的个人知识库笔记消息。

    格式：
      # 内容
      # [question] 内容
      # [fact] #Python #AI 内容
    """
    if not text:
        return None

    stripped = text.lstrip()
    if not stripped.startswith("#"):
        return None

    body = stripped[1:].lstrip()
    note_type = "idea"

    type_match = re.match(r"^\[([^\]]+)\]\s*", body)
    if type_match:
        candidate = type_match.group(1).strip().lower()
        if candidate in VALID_NOTE_TYPES:
            note_type = candidate
        body = body[type_match.end():]

    topics: list[str] = []

    def _collect_topic(match: re.Match[str]) -> str:
        topic = normalize_topic(match.group(1))
        if topic and topic not in topics:
            topics.append(topic)
        return ""

    body = re.sub(r"(?<!\w)#([A-Za-z0-9_\u4e00-\u9fff-]+)", _collect_topic, body)
    content = " ".join(body.split()).strip()
    if not content:
        return None
    return content, note_type, topics


def _coerce_pkb_limit(limit: Any, default: int = 5) -> int:
    try:
        value = int(limit if limit is not None else default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 10))


def _normalize_pkb_result_item(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    content_text = str(item.get("content") or item.get("text") or item.get("title") or "")
    return {
        "title": str(item.get("title") or content_text[:40] or "笔记"),
        "content": content_text,
        "topics": item.get("topics") or [],
        "type": item.get("type") or item.get("note_type") or "",
        "url": str(item.get("url") or item.get("link") or ""),
        "created_at": item.get("created_at") or item.get("createdAt") or "",
    }


def format_pkb_result_items(items: list[dict], limit: int = 5) -> list[str]:
    lines: list[str] = []
    for item in items[:_coerce_pkb_limit(limit)]:
        title = str(item.get("title") or "笔记")
        note_type = str(item.get("type") or "idea")
        topics = normalize_topics(item.get("topics") or [])
        content = str(item.get("content") or "").strip()
        snippet = content[:120] + "…" if len(content) > 120 else content
        url = str(item.get("url") or "").strip()

        meta = [note_type]
        if topics:
            meta.append(" / ".join(topics))
        lines.append(f"- [{' · '.join(meta)}] **{title}**")
        if snippet:
            lines.append(f"  {snippet}")
        if url:
            lines.append(f"  🔗 {url}")

    return lines


def _normalize_pkb_result_payload(data: Any) -> tuple[str, list[dict]]:
    summary = ""
    results: list[dict] = []

    if isinstance(data, dict):
        summary = str(
            data.get("summary")
            or data.get("answer")
            or data.get("message")
            or data.get("title")
            or ""
        )
        raw_results = (
            data.get("results")
            or data.get("records")
            or data.get("notes")
            or data.get("hits")
            or data.get("data")
            or data.get("items")
            or []
        )
        if isinstance(raw_results, dict):
            raw_results = (
                raw_results.get("results")
                or raw_results.get("records")
                or raw_results.get("notes")
                or raw_results.get("hits")
                or raw_results.get("data")
                or raw_results.get("items")
                or []
            )
        if isinstance(raw_results, list):
            for item in raw_results:
                normalized = _normalize_pkb_result_item(item)
                if normalized:
                    results.append(normalized)
    elif isinstance(data, list):
        for item in data:
            normalized = _normalize_pkb_result_item(item)
            if normalized:
                results.append(normalized)

    return summary, results


async def check_pkb_health() -> dict:
    client: PkbClient = get_pkb_client()
    return await client.health()


async def forward_to_pkb_result(content: str, note_type: str, topics: list[str]) -> dict:
    """转发笔记到已部署的 PKB 接口。"""
    client: PkbClient = get_pkb_client()
    return await client.save(
        content,
        source="luck-agent",
        note_type=note_type,
        topics=topics,
    )


async def forward_to_pkb(content: str, note_type: str, topics: list[str]) -> bool:
    result = await forward_to_pkb_result(content, note_type, topics)
    return bool(result.get("ok"))


async def search_pkb(query: str, limit: int = 5) -> dict:
    """检索已部署的 PKB 接口。"""
    query = query.strip()
    if not query:
        return {"error": "缺少 query"}

    limit = _coerce_pkb_limit(limit)

    client: PkbClient = get_pkb_client()
    data = await client.search(query, limit=limit)
    if not data.get("ok", True):
        return data

    summary, results = _normalize_pkb_result_payload(data)

    return {
        "query": query,
        "summary": summary,
        "results": results[:limit],
        "count": len(results),
        "source": "pkb",
    }

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
        self._user_locks: dict[str, asyncio.Lock] = {}
        from tools.search_tools import SearchTools
        self.searcher = SearchTools()

        # 合并所有工具 schema
        from tools.github_tools import GITHUB_TOOL_SCHEMAS
        from tools.shell_tools import SHELL_TOOL_SCHEMAS
        from tools.search_tools import SEARCH_TOOL_SCHEMAS
        from core.scheduler import SCHEDULE_TOOL_SCHEMAS
        self.all_tools = GITHUB_TOOL_SCHEMAS + SHELL_TOOL_SCHEMAS + SEARCH_TOOL_SCHEMAS + SCHEDULE_TOOL_SCHEMAS + PKB_TOOL_SCHEMAS + [
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
        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            await self._handle_serialized(
                user_id,
                chat_id,
                message_id,
                text,
                model_override=model_override,
            )

    async def _handle_serialized(
        self,
        user_id: str,
        chat_id: str,
        message_id: str,
        text: str,
        model_override: str = "",
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

        # 3. 确认进入模型处理后先持久化用户消息，再构建上下文。
        from core.memory import Message as Msg
        self.memory.add_message(Msg(user_id, "user", text))

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

        # 5. 拼接消息（history 已包含当前用户消息）
        recent_history = history[-6:] if len(history) > 6 else history
        messages = recent_history

        # 6. 工具调用循环
        final_text   = ""
        tool_rounds  = 0
        all_tool_results: list[dict] = []

        while tool_rounds < MAX_TOOL_ROUNDS:
            log.info(
                "react_loop_context",
                user_id=user_id[:8],
                chat_id=chat_id[:8],
                message_id=message_id[:12],
                round=tool_rounds + 1,
                count=len(messages),
                roles=[message.get("role", "?") for message in messages],
                content_lengths=[
                    len(str(message.get("content", "")))
                    for message in messages
                ],
                history_messages=len(recent_history),
                tool_result_messages=sum(
                    1
                    for message in messages
                    if message.get("role") == "tool"
                    or str(message.get("content", "")).startswith(
                        "工具执行完毕，结果如下"
                    )
                ),
            )
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
                error_text = f"模型调用失败：{type(e).__name__}: {e}"
                self.memory.add_message(
                    Msg(user_id, "assistant", error_text, model=model)
                )
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
                self.memory.add_message(Msg(user_id, "assistant", final_text,
                                           model=result["model"], tokens=result["tokens"]))

                reply_card = self.card.agent_reply(
                    text=final_text,
                    model=result["model"],
                    elapsed=elapsed,
                )
                blog_result = next(
                    (
                        tr["result"]
                        for tr in all_tool_results
                        if tr.get("tool") == "create_blog_post" and isinstance(tr.get("result"), dict)
                        and not tr["result"].get("error")
                    ),
                    None,
                )
                if blog_result:
                    reply_card = self.card.blog_publish(blog_result)

                # 5. 发送回复卡片
                await self.reply(
                    chat_id,
                    text=final_text,
                    card=reply_card,
                )

                log.info("message_handled",
                         user_id=user_id[:8],
                         model=result["model"],
                         tool_rounds=tool_rounds,
                         elapsed=round(elapsed, 2))
                return

        # 超过工具调用轮数上限
        limit_text = (
            f"工具调用轮数超限：已执行 {MAX_TOOL_ROUNDS} 轮，"
            "请尝试简化问题。"
        )
        self.memory.add_message(
            Msg(user_id, "assistant", limit_text, model=model)
        )
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
            try:
                result = await self.github.create_blog_post(**args, shell=self.shell)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}
            self.memory.log_github(user_id, args.get("repo",""), "create_blog_post",
                                   args.get("title",""), str(result))
            # VPS 路径下 trigger_workflow 由模型决策是否调用
            return result

        elif name == "list_blog_posts":
            try:
                return await self.github.list_blog_posts(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}

        elif name == "trigger_workflow":
            try:
                result = await self.github.trigger_workflow(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}
            self.memory.log_github(user_id, args.get("repo",""), "trigger_workflow",
                                   str(args), str(result))
            return result

        elif name == "list_workflow_runs":
            try:
                return await self.github.list_workflow_runs(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}

        elif name == "list_items":
            try:
                return await self.github.list_items(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}

        elif name == "create_issue":
            try:
                return await self.github.create_issue(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}

        elif name == "get_blog_post":
            try:
                return await self.github.get_blog_post(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}

        elif name == "get_file":
            try:
                return await self.github.get_file(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}

        elif name == "update_file":
            try:
                return await self.github.update_file(**args)
            except Exception as e:
                return {"error": f"{e} | {self.github.explain_error(str(e))}"}

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
            if result.get("returncode", 0) != 0:
                hint = self.shell.explain_permission_issue(result.get("stderr", ""))
                if hint and "error" not in result:
                    result["hint"] = hint
            return result

        elif name == "list_files":
            return self.file_mgr.list_dir(args.get("path", ""))

        elif name == "read_file":
            return self.file_mgr.read_file(args.get("path", ""))

        elif name == "write_file":
            return self.file_mgr.write_file(args.get("path",""), args.get("content",""))

        elif name == "delete_file":
            return self.file_mgr.delete(args.get("path",""))

        # ── 搜索工具 ──
        elif name == "search_web":
            result = await self.searcher.search(args.get("query", ""))
            return result

        elif name.startswith("pkb_"):
            client = get_pkb_client()
            try:
                if name == "pkb_save":
                    return await client.save(
                        (args.get("content") or "").strip(),
                        source=(args.get("source") or "luck-agent").strip(),
                        note_type=(args.get("type") or "fact").strip(),
                        topics=normalize_topics(args.get("topics") or []),
                    )
                if name == "pkb_search":
                    kwargs = {"limit": args.get("limit", 5)}
                    if args.get("source"):
                        kwargs["source"] = str(args["source"]).strip()
                    return await client.search((args.get("query") or "").strip(), **kwargs)
                if name == "pkb_get":
                    return await client.get((args.get("id") or "").strip())
                if name == "pkb_list":
                    return await client.list(
                        limit=args.get("limit", 50),
                        offset=args.get("offset", 0),
                        note_type=args.get("type"),
                        topics=normalize_topics(args.get("topics") or []),
                        from_=args.get("from"),
                        to=args.get("to"),
                        include_deleted=bool(args.get("include_deleted", False)),
                    )
                if name == "pkb_update":
                    return await client.update(
                        (args.get("id") or "").strip(),
                        content=args.get("content"),
                        note_type=args.get("type"),
                        topics=normalize_topics(args["topics"]) if "topics" in args else None,
                        summary=args.get("summary"),
                    )
                if name == "pkb_delete":
                    return await client.delete((args.get("id") or "").strip())
                if name == "pkb_restore":
                    return await client.restore((args.get("id") or "").strip())
            except (TypeError, ValueError) as exc:
                return {"ok": False, "code": "invalid_arguments", "error": str(exc)}

        elif name == "schedule_task":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            mode = (args.get("mode") or "").strip().lower()
            schedule = (args.get("schedule") or "").strip()
            name_ = (args.get("name") or "").strip()
            prompt = (args.get("prompt") or "").strip()
            if not all([mode, schedule, name_, prompt]):
                return {"error": "缺少必要参数"}
            try:
                if mode == "cron":
                    task = self.scheduler.add_cron(user_id, chat_id, name_, prompt, schedule)
                elif mode == "interval":
                    task = self.scheduler.add_interval(user_id, chat_id, name_, prompt, int(schedule))
                else:
                    return {"error": f"未知模式：{mode}"}
            except Exception as e:
                return {"error": str(e)}
            return {"created": True, "task": task.to_dict()}

        elif name == "list_schedules":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            return {"tasks": [t.to_dict() for t in self.scheduler.list_user(user_id)]}

        elif name == "cancel_schedule":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            return {"deleted": self.scheduler.cancel(args.get("task_id", "")), "task_id": args.get("task_id", "")}

        elif name == "pause_schedule":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            return {"updated": self.scheduler.pause(args.get("task_id", "")), "task_id": args.get("task_id", "")}

        elif name == "resume_schedule":
            if not self.scheduler:
                return {"error": "调度器未初始化"}
            return {"updated": self.scheduler.resume(args.get("task_id", "")), "task_id": args.get("task_id", "")}

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
                deploy_text = "🚀 已触发部署" if res.get("deploy_triggered") else "⚠️ 部署未触发"
                deploy_error = res.get("deploy_error", "")
                lines.append(
                    f"✅ 博文已{action}：`{res.get('content_path') or res.get('path','')}`\n"
                    f"   commit `{res.get('commit','')}` · {deploy_text}"
                )
                if deploy_error:
                    lines.append(f"   ⚠️ {deploy_error[:180]}")
            elif tool == "trigger_workflow":
                lines.append(f"🚀 已触发 workflow：`{res.get('workflow','')}` @ `{res.get('ref','')}`")
            elif tool == "run_shell":
                rc = res.get("returncode", -1)
                icon = "✅" if rc == 0 else "❌"
                out = (res.get("stdout") or "").strip()[:200]
                hint = res.get("hint", "")
                msg = f"{icon} Shell 返回码 {rc}" + (f"\n```\n{out}\n```" if out else "")
                if hint:
                    msg += f"\n💡 {hint}"
                lines.append(msg)
            elif tool == "create_issue":
                lines.append(f"✅ Issue #{res.get('number','')} 已创建：{res.get('url','')}")
            elif tool == "update_file":
                lines.append(f"✅ 文件已更新：`{res.get('path','')}` commit `{res.get('commit','')}`")
            elif tool == "get_blog_post":
                content = (res.get("content") or "")[:150]
                lines.append(f"📄 已读取博文 `{res.get('path','')}`：\n{content}…")
            elif tool == "search_web":
                summary = (res.get("summary") or "")[:500]
                backend = res.get("backend", "")
                items = res.get("results", [])[:3]
                item_bits = []
                for item in items:
                    title = item.get("title", "")
                    url = item.get("url", "")
                    if title and url:
                        item_bits.append(f"- [{title}]({url})")
                lines.append(
                    f"🔎 搜索完成"
                    + (f"（{backend}）" if backend else "")
                    + (f"：{summary}" if summary else "")
                )
                if item_bits:
                    lines.append("\n".join(item_bits))
            elif tool in ("pkb_search", "pkb_list"):
                summary = (res.get("summary") or "")[:500]
                items = res.get("results", [])[:3]
                lines.append(
                    f"🗃️ 个人知识库检索完成"
                    + (f"：{summary}" if summary else "")
                )
                item_bits = format_pkb_result_items(items, limit=3)
                if item_bits:
                    lines.append("\n".join(item_bits))
            elif tool == "pkb_save":
                if res.get("idempotent"):
                    lines.append("🗃️ 知识库中已有该内容")
                else:
                    lines.append(f"🗃️ 个人知识库已记录：`{res.get('type', 'fact')}`")
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
