"""
cards/builder.py — Lark 消息卡片构建器
使用 Lark Card 2.0 JSON 格式构建交互式卡片。
支持：任务进度卡片 / GitHub Actions 状态 / Shell 输出 / 文件列表 / 错误卡片
"""
from __future__ import annotations

import json
import time
from typing import Any

from core.topics import normalize_topics


# ── 颜色语义 ─────────────────────────────────────────────────────────────────
STATUS_COLOR = {
    "success":   "green",
    "done":      "green",
    "completed": "green",
    "failure":   "red",
    "failed":    "red",
    "cancelled": "grey",
    "running":   "blue",
    "pending":   "yellow",
    "retrying":  "orange",
    "skipped":   "grey",
}

STATUS_EMOJI = {
    "success": "✅", "done": "✅", "completed": "✅",
    "failure": "❌", "failed": "❌",
    "running": "⏳", "pending": "🕐",
    "retrying": "🔄", "cancelled": "⏹️",
}


def _tag(text: str, color: str = "blue") -> dict:
    # Card 2.0 不支持 tag:text，统一用 markdown
    return {"tag": "markdown", "content": text}


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def _divider() -> dict:
    return {"tag": "hr"}


def _format_ts(ts: float) -> str:
    if not ts or ts <= 0:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _format_eta(ts: float) -> str:
    if not ts or ts <= 0:
        return ""
    delta = int(ts - time.time())
    if delta <= 0:
        return "即将触发"
    if delta < 60:
        return f"{delta} 秒后"
    if delta < 3600:
        return f"{delta // 60} 分钟后"
    return f"{delta // 3600} 小时后"


def _button(text: str, action_type: str, value: dict, style: str = "default") -> dict:
    return {
        "tag":   "button",
        "text":  {"tag": "plain_text", "content": text},
        "type":  style,
        "behaviors": [{"type": "callback", "value": {**value, "_action": action_type}}],
    }


class CardBuilder:
    """构建各类 Lark 消息卡片。"""

    # ── 通用回复卡片 ──────────────────────────────────────────────────
    @staticmethod
    def agent_reply(
        text: str,
        model: str = "",
        elapsed: float = 0,
        task_id: str = "",
        user_id: str = "",
    ) -> dict:
        """AI 回复卡片。"""
        elements = [{"tag": "markdown", "content": text}]

        footer_parts = []
        if model:
            footer_parts.append(f"🤖 {model.split('/')[-1]}")
        if elapsed:
            footer_parts.append(f"⏱ {elapsed:.1f}s")
        if task_id:
            footer_parts.append(f"📋 #{task_id}")

        if footer_parts:
            elements.append(_divider())
            elements.append({
                "tag": "markdown",
                "content": f"<font color='grey'>{' · '.join(footer_parts)}</font>",
            })

        return {
            "schema": "2.0",
            "body":   {"elements": elements},
            "header": {
                "title": {"tag": "plain_text", "content": "Agent 回复"},
                "template": "blue",
            },
        }

    # ── Shell 输出卡片 ─────────────────────────────────────────────────
    @staticmethod
    def shell_output(
        command: str,
        stdout: str,
        returncode: int,
        elapsed: float,
        truncated: bool = False,
    ) -> dict:
        success = returncode == 0
        color   = "green" if success else "red"
        icon    = "✅" if success else "❌"

        output_display = stdout or "（无输出）"
        if truncated:
            output_display += "\n\n⚠️ 输出已截断"

        elements = [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**命令**\n`{command[:100]}`"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**状态**\n{icon} 返回码 {returncode}"}},
                ],
            },
            _divider(),
            {
                "tag":     "markdown",
                "content": f"```\n{output_display}\n```",
            },
            _divider(),
            {"tag": "markdown", "content": f"<font color='grey'>⏱ 耗时 {elapsed}s</font>"},
        ]

        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": f"{icon} Shell 执行结果"},
                "template": color,
            },
            "body": {"elements": elements},
        }

    # ── GitHub Actions 状态卡片 ─────────────────────────────────────────
    @staticmethod
    def workflow_runs(repo: str, runs: list[dict]) -> dict:
        elements = [
            {"tag": "markdown", "content": f"**仓库：** `{repo}`"},
            _divider(),
        ]

        for r in runs[:8]:
            status     = r.get("conclusion") or r.get("status", "unknown")
            color      = STATUS_COLOR.get(status, "grey")
            emoji      = STATUS_EMOJI.get(status, "❓")
            created    = r.get("created_at", "")[:16].replace("T", " ")
            elements.append({
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 3,
                        "elements": [
                            {"tag": "markdown",
                             "content": f"**{r['name']}** — {r.get('branch','')}\n{created}"},
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {"tag": "markdown",
                             "content": f"{emoji} {status}"},
                        ],
                    },
                ],
            })

        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": "🔧 GitHub Actions"},
                "template": "blue",
            },
            "body": {"elements": elements},
        }

    # ── Blog 文章列表卡片 ─────────────────────────────────────────────
    @staticmethod
    def blog_posts(repo: str, posts: list[dict]) -> dict:
        elements = [
            {"tag": "markdown", "content": f"**博客仓库：** `{repo}`  共 {len(posts)} 篇文章"},
            _divider(),
        ]

        for p in posts[:10]:
            size_kb = round(p.get("size", 0) / 1024, 1)
            elements.append({
                "tag": "markdown",
                "content": f"📄 [{p['name']}]({p.get('html_url', '#')})  `{size_kb}KB`",
            })

        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": "📝 Hugo 博文列表"},
                "template": "green",
            },
            "body": {"elements": elements},
        }

    # ── 博客发布结果卡片 ─────────────────────────────────────────────
    @staticmethod
    def blog_publish(result: dict) -> dict:
        action = result.get("action", "create")
        path = result.get("path", "")
        commit = result.get("commit", "")
        url = result.get("html_url", "")
        deploy_triggered = result.get("deploy_triggered", False)

        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**动作**\n`{action}`"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**部署**\n{'已触发' if deploy_triggered else '未触发'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**路径**\n`{path}`"}},
            ]},
        ]
        if commit or url:
            meta = []
            if commit:
                meta.append(f"提交 `{commit}`")
            if url:
                meta.append(f"链接 {url}")
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": " · ".join(meta)})

        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "📝 博客发布"},
                "template": "green" if action == "create" else "turquoise",
            },
            "body": {"elements": elements},
        }

    # ── 搜索结果卡片 ─────────────────────────────────────────────────
    @staticmethod
    def search_results(query: str, result: dict) -> dict:
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**查询**\n`{query}`"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**后端**\n`{result.get('backend', '') or 'unknown'}`"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**结果**\n{len(result.get('results', [])[:5])} 条"}},
            ]},
        ]

        summary = result.get("summary", "")
        source = result.get("source", "")
        items = result.get("results", [])[:5]
        if summary:
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": f"📋 {summary[:600]}"})
        if source:
            elements.append({"tag": "markdown", "content": f"🔗 来源：{source}"})

        if items:
            elements.append(_divider())
            for i, item in enumerate(items, 1):
                title = item.get("title", "")
                url = item.get("url", "")
                desc = item.get("description", "")[:180]
                content = f"{i}. [{title}]({url})"
                if desc:
                    content += f"\n   {desc}"
                elements.append({"tag": "markdown", "content": content})

        if not summary and not items:
            elements.append({"tag": "markdown", "content": "_未找到相关结果_"})

        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "🔎 搜索"},
                "template": "blue",
            },
            "body": {"elements": elements},
        }

    # ── 个人知识库结果卡片 ───────────────────────────────────────────
    @staticmethod
    def pkb_results(query: str, result: dict) -> dict:
        items = result.get("results", [])[:5]
        summary = result.get("summary", "")
        count = result.get("count", len(items))
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**查询**\n`{query}`"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**结果**\n{count} 条"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**来源**\n`PKB`"}},
            ]},
        ]

        if summary:
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": f"📋 {summary[:500]}"})

        if items:
            elements.append(_divider())
            for i, item in enumerate(items, 1):
                title = item.get("title", "") or "笔记"
                note_type = item.get("type", "") or "idea"
                topics = normalize_topics(item.get("topics") or [])
                meta = [note_type]
                if topics:
                    meta.append(" / ".join(topics))
                content = item.get("content", "")[:180]
                url = item.get("url", "")
                header = f"{i}. **{title}**"
                if meta:
                    header += f"  <font color='grey'>({ ' · '.join(meta) })</font>"
                elements.append({"tag": "markdown", "content": header})
                if url:
                    elements.append({"tag": "markdown", "content": f"🔗 [打开原文]({url})"})
                if content:
                    elements.append({"tag": "markdown", "content": f"<font color='grey'>{content}</font>"})
        else:
            elements.append({"tag": "markdown", "content": "_未找到相关笔记_"})

        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "🗃️ 个人知识库"},
                "template": "purple",
            },
            "body": {"elements": elements},
        }

    # ── 个人知识库录入结果卡片 ───────────────────────────────────────
    @staticmethod
    def pkb_recorded(content: str, note_type: str, topics: list[str], ok: bool = True, detail: str = "") -> dict:
        color = "green" if ok else "red"
        icon = "✅" if ok else "❌"
        normalized_topics = normalize_topics(topics)
        topic_text = " / ".join(normalized_topics) if normalized_topics else "无"
        snippet = content[:160] + "…" if len(content) > 160 else content
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**结果**\n{icon} {'已记录' if ok else '记录失败'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**类型**\n{note_type}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**话题**\n{topic_text}"}},
            ]},
            _divider(),
            {"tag": "markdown", "content": snippet or "_空内容_"},
        ]
        if detail:
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": f"<font color='grey'>{detail[:180]}</font>"})

        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "🗃️ 个人知识库录入"},
                "template": color,
            },
            "body": {"elements": elements},
        }

    # ── 定时任务列表卡片 ─────────────────────────────────────────────
    @staticmethod
    def schedule_list(tasks: list[dict]) -> dict:
        def _task_sort_key(task: dict) -> tuple:
            enabled_rank = 0 if task.get("enabled") else 1
            next_run = float(task.get("next_run", 0) or 0)
            created_at = float(task.get("created_at", 0) or 0)
            return (enabled_rank, next_run if next_run > 0 else float("inf"), created_at)

        ordered_tasks = sorted(tasks, key=_task_sort_key)
        enabled_count = sum(1 for t in ordered_tasks if t.get("enabled"))
        upcoming = [float(t.get("next_run", 0) or 0) for t in ordered_tasks if float(t.get("next_run", 0) or 0) > 0]
        soonest = min(upcoming) if upcoming else 0
        soonest_ts = _format_ts(soonest)
        soonest_eta = _format_eta(soonest)
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务数**\n{len(tasks)} 条"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**启用**\n{enabled_count} 条"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**模式**\ncron / interval"}},
            ]},
        ]
        if soonest_ts:
            elements.append({"tag": "markdown", "content": f"<font color='grey'>最近触发：{soonest_ts} · {soonest_eta or '—'}</font>"})

        for t in ordered_tasks[:8]:
            icon = "✅" if t.get("enabled") else "⏸"
            mode = t.get("mode", "")
            if mode == "cron":
                schedule = t.get("schedule", "")
            else:
                try:
                    seconds = int(t.get("schedule", 0))
                    schedule = f"每{seconds}秒" if seconds < 60 else f"每{seconds // 60}分钟"
                except Exception:
                    schedule = str(t.get("schedule", ""))
            next_run = _format_ts(float(t.get("next_run", 0) or 0))
            eta = _format_eta(float(t.get("next_run", 0) or 0))
            prompt = t.get("prompt", "")
            prompt = prompt[:60] + "…" if len(prompt) > 60 else prompt

            elements.append({
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 3,
                        "elements": [
                            {"tag": "markdown", "content": f"**{icon} {t.get('name', '')}**"},
                            {"tag": "markdown", "content": f"`#{t.get('id', '')}` · {mode} · {schedule}"},
                            {"tag": "markdown", "content": f"<font color='grey'>下次：{next_run or '未设置'} · {eta or '—'}</font>"},
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {"tag": "markdown", "content": f"{t.get('run_count', 0)} 次"},
                        ],
                    },
                ],
            })
            if prompt:
                elements.append({"tag": "markdown", "content": f"<font color='grey'>{prompt}</font>"})

        if not ordered_tasks:
            elements.append({"tag": "markdown", "content": "_暂无定时任务_"})

        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "📅 定时任务"},
                "template": "purple",
            },
            "body": {"elements": elements},
        }

    # ── 定时任务创建成功卡片 ─────────────────────────────────────────
    @staticmethod
    def schedule_created(task: dict) -> dict:
        mode = task.get("mode", "")
        schedule = task.get("schedule", "")
        if mode == "interval":
            try:
                seconds = int(schedule)
                schedule = f"每{seconds}秒" if seconds < 60 else f"每{seconds // 60}分钟"
            except Exception:
                schedule = str(schedule)
        next_run = _format_ts(float(task.get("next_run", 0) or 0))
        eta = _format_eta(float(task.get("next_run", 0) or 0))

        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务**\n{task.get('name', '')}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**模式**\n{mode}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**计划**\n{schedule}"}},
            ]},
        ]

        if next_run:
            elements.append({"tag": "markdown", "content": f"<font color='grey'>下次触发：{next_run} · {eta or '—'}</font>"})

        prompt = task.get("prompt", "")
        if prompt:
            prompt = prompt[:120] + "…" if len(prompt) > 120 else prompt
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": f"<font color='grey'>{prompt}</font>"})

        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "📅 定时任务已创建"},
                "template": "green",
            },
            "body": {"elements": elements},
        }

    # ── 定时任务动作确认卡片 ─────────────────────────────────────────
    @staticmethod
    def schedule_action(action: str, task_id: str, ok: bool, detail: str = "", task: dict | None = None) -> dict:
        title = {
            "pause": "📅 定时任务已暂停" if ok else "📅 暂停失败",
            "resume": "📅 定时任务已恢复" if ok else "📅 恢复失败",
            "cancel": "📅 定时任务已删除" if ok else "📅 删除失败",
        }.get(action, "📅 定时任务操作")
        status = "成功" if ok else "失败"
        icon = "✅" if ok else "❌"
        task_name = task.get("name", "") if task else ""
        enabled = task.get("enabled") if task else None
        next_run = _format_ts(float(task.get("next_run", 0) or 0)) if task else ""
        eta = _format_eta(float(task.get("next_run", 0) or 0)) if task else ""
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务**\n`#{task_id}`"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**结果**\n{icon} {status}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**动作**\n{action}"}},
            ]},
        ]
        if task_name:
            elements.append({"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**名称**\n{task_name}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**启用**\n{'是' if enabled else '否'}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**下次**\n{next_run or '—'}"}},
            ]})
            if eta:
                elements.append({"tag": "markdown", "content": f"<font color='grey'>ETA：{eta}</font>"})
        if detail:
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": f"<font color='grey'>{detail[:120]}</font>"})
        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green" if ok else "red",
            },
            "body": {"elements": elements},
        }

    # ── 健康诊断卡片 ──────────────────────────────────────────────────
    @staticmethod
    def health_status(details: dict) -> dict:
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**WS**\n{details.get('ws_online', 'unknown')}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**备份**\n{details.get('backup_count', 0)} 个"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**DB**\n`{details.get('db_path', '')}`"}},
            ]},
        ]
        meta_bits = []
        if details.get("backup_dir"):
            meta_bits.append(f"备份目录 `{details.get('backup_dir')}`")
        if details.get("upload_dir"):
            meta_bits.append(f"上传目录 `{details.get('upload_dir')}`")
        if details.get("shell_work_dir"):
            meta_bits.append(f"Shell 工作区 `{details.get('shell_work_dir')}`")
        if meta_bits:
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": " · ".join(meta_bits)})
        if details.get("hint"):
            elements.append({"tag": "markdown", "content": details["hint"]})

        return {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "🩺 健康"},
                "template": "turquoise",
            },
            "body": {"elements": elements},
        }

    # ── 任务状态卡片 ──────────────────────────────────────────────────
    @staticmethod
    def task_status(task_id: str, task_type: str, status: str,
                    result: Any = None, error: str = "") -> dict:
        color = STATUS_COLOR.get(status, "grey")
        emoji = STATUS_EMOJI.get(status, "❓")

        elements: list[dict] = [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**任务 ID**\n#{task_id}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**类型**\n{task_type}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**状态**\n{emoji} {status}"}},
                ],
            },
        ]

        if result:
            elements.append(_divider())
            result_str = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)
            if len(result_str) > 800:
                result_str = result_str[:800] + "\n…（已截断）"
            elements.append({"tag": "markdown", "content": f"**结果**\n```json\n{result_str}\n```"})

        if error:
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": f"**错误**\n```\n{error[:400]}\n```"})

        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": f"{emoji} 任务 #{task_id}"},
                "template": color,
            },
            "body": {"elements": elements},
        }

    # ── 文件列表卡片 ──────────────────────────────────────────────────
    @staticmethod
    def file_list(files: list[dict], title: str = "VPS 文件列表") -> dict:
        elements: list[dict] = []
        for f in files[:15]:
            sz   = f.get("size_kb", 0)
            name = f.get("name", "")
            mod  = time.strftime("%m-%d %H:%M", time.localtime(f.get("modified", 0)))
            elements.append({
                "tag": "markdown",
                "content": f"📁 `{name}` — {sz}KB  _{mod}_",
            })
        if not elements:
            elements.append({"tag": "markdown", "content": "_目录为空_"})

        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {"elements": elements},
        }

    # ── 文件收到确认卡片 ──────────────────────────────────────────────
    @staticmethod
    def file_received(file_name: str, size: int, local_path: str) -> dict:
        size_str = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.2f} MB"
        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": "📥 文件已接收"},
                "template": "green",
            },
            "body": {
                "elements": [
                    {"tag": "div", "fields": [
                        {"is_short": True, "text": {"tag": "lark_md",
                                                    "content": f"**文件名**\n{file_name}"}},
                        {"is_short": True, "text": {"tag": "lark_md",
                                                    "content": f"**大小**\n{size_str}"}},
                        {"is_short": False, "text": {"tag": "lark_md",
                                                     "content": f"**保存路径**\n`{local_path}`"}},
                    ]},
                ]
            },
        }

    # ── 错误卡片 ──────────────────────────────────────────────────────
    @staticmethod
    def error(message: str, detail: str = "") -> dict:
        elements = [{"tag": "markdown", "content": f"❌ **{message}**"}]
        if detail:
            elements.append({"tag": "markdown", "content": f"```\n{detail[:500]}\n```"})
        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": "⚠️ 错误"},
                "template": "red",
            },
            "body": {"elements": elements},
        }

    # ── 系统状态卡片 ──────────────────────────────────────────────────
    @staticmethod
    def system_status(memory_stats: dict, task_summary: list[dict],
                      disk: dict | None = None,
                      mem: dict | None = None,
                      procs: str = "") -> dict:
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**消息**\n{memory_stats.get('messages', 0)}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**任务**\n{memory_stats.get('tasks', 0)}"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**用户**\n{memory_stats.get('users', 0)}"}},
            ]},
        ]

        extra_bits = []
        if memory_stats.get("ws_online") is not None:
            extra_bits.append(f"WS {memory_stats.get('ws_online')}")
        if memory_stats.get("ws_last_ok") is not None:
            extra_bits.append(f"心跳 {memory_stats.get('ws_last_ok')}s 前")
        if memory_stats.get("backup_count") is not None:
            extra_bits.append(f"备份 {memory_stats.get('backup_count')} 个")
        if extra_bits:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "fields": [
                {"is_short": False, "text": {"tag": "lark_md", "content": " · ".join(extra_bits)}},
            ]})

        if mem:
            elements.append({"tag": "hr"})
            mem_content = f"**内存** {mem.get('used', '?')} / {mem.get('total', '?')}，剩余 {mem.get('free', '?')}"
            if procs:
                mem_content += f" | 进程 {procs}"
            elements.append({"tag": "div", "fields": [
                {"is_short": False, "text": {"tag": "lark_md", "content": mem_content}},
            ]})

        if disk:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "fields": [
                {"is_short": False, "text": {"tag": "lark_md",
                                            "content": f"**磁盘** 已用 {disk.get('used', '?')}，剩余 {disk.get('free', '?')}"}},
            ]})

        if task_summary:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "fields": [
                {"is_short": False, "text": {"tag": "lark_md", "content": "**近期任务**"}},
            ]})
            for t in task_summary[:5]:
                emoji = STATUS_EMOJI.get(t.get("status", ""), "?")
                elements.append({"tag": "div", "fields": [
                    {"is_short": False, "text": {"tag": "lark_md",
                                                "content": f"{emoji} #{t['task_id']} {t['type']} - {t['status']}"}},
                ]})

        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": "📊 状态"},
                "template": "turquoise",
            },
            "body": {"elements": elements},
        }

    @staticmethod
    def to_json(card: dict) -> str:
        return json.dumps(card, ensure_ascii=False)


