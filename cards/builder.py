"""
cards/builder.py — Lark 消息卡片构建器
使用 Lark Card 2.0 JSON 格式构建交互式卡片。
支持：任务进度卡片 / GitHub Actions 状态 / Shell 输出 / 文件列表 / 错误卡片
"""
from __future__ import annotations

import json
import time
from typing import Any


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
                      disk: dict | None = None) -> dict:
        elements = [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md",
                                            "content": f"**消息总数**\n{memory_stats.get('messages', 0)}"}},
                {"is_short": True, "text": {"tag": "lark_md",
                                            "content": f"**任务总数**\n{memory_stats.get('tasks', 0)}"}},
                {"is_short": True, "text": {"tag": "lark_md",
                                            "content": f"**活跃用户**\n{memory_stats.get('users', 0)}"}},
            ]},
        ]

        if disk:
            elements.append(_divider())
            elements.append({
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**已用**\n{disk.get('used', '?')}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                                                "content": f"**剩余**\n{disk.get('free', '?')}"}},
                ],
            })

        if task_summary:
            elements.append(_divider())
            elements.append({"tag": "markdown", "content": "**近期任务**"})
            for t in task_summary[:5]:
                emoji = STATUS_EMOJI.get(t.get("status", ""), "❓")
                elements.append({
                    "tag": "markdown",
                    "content": f"{emoji} `#{t['task_id']}` {t['type']} — {t['status']}",
                })

        return {
            "schema": "2.0",
            "header": {
                "title":    {"tag": "plain_text", "content": "📊 系统状态"},
                "template": "blue",
            },
            "body": {"elements": elements},
        }

    @staticmethod
    def to_json(card: dict) -> str:
        return json.dumps(card, ensure_ascii=False)
