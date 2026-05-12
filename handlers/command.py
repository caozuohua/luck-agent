"""
handlers/command.py — 直接指令处理器（大模型无关）
/cmd, /sh, /file, /git, /task, /status 等指令
即使 Gemini 不可用，这些指令仍然正常工作。
"""
from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from tools.shell_tools import ShellExecutor, FileManager
    from tools.file_bridge import FileBridge
    from tools.github_tools import GitHubClient
    from core.memory import Memory
    from core.task_queue import TaskQueue
    from cards.builder import CardBuilder

log = structlog.get_logger()

HELP_TEXT = """
**⚡ 直接指令（大模型无关）**

**Shell 执行**
`/sh <命令>` — 执行 shell 命令
`/sh! <命令>` — 确认执行危险命令
`/script` + 代码块 — 执行多行脚本

**文件操作**
`/ls [路径]` — 列出文件
`/cat <路径>` — 读取文件内容
`/rm <路径>` — 删除文件
`/files` — 列出已上传文件
`/send <路径>` — 发送 VPS 文件到 Lark

**GitHub 快捷**
`/deploy [repo]` — 触发 deploy.yml
`/runs [repo]` — 查看 Actions 运行
`/posts [repo]` — 列出博文

**任务管理**
`/task <id>` — 查看任务状态
`/tasks` — 查看我的任务列表
`/cancel <id>` — 取消任务

**系统**
`/status` — 系统状态
`/mem` — 查看 + 清除对话记忆
`/help` — 显示本帮助
""".strip()


class CommandHandler:
    """解析 / 前缀指令，不经过大模型。"""

    def __init__(
        self,
        shell: "ShellExecutor",
        files: "FileManager",
        bridge: "FileBridge",
        github: "GitHubClient",
        memory: "Memory",
        queue: "TaskQueue",
        card: type["CardBuilder"],
        lark_reply_fn,               # async fn(chat_id, text=None, card=None)
        hugo_repo: str = "",
    ) -> None:
        self.shell    = shell
        self.files    = files
        self.bridge   = bridge
        self.github   = github
        self.memory   = memory
        self.queue    = queue
        self.card     = card
        self.reply    = lark_reply_fn
        self.hugo_repo = hugo_repo

        # 待确认的危险命令（防误删）
        self._pending_dangerous: dict[str, str] = {}  # user_id → command

    def is_command(self, text: str) -> bool:
        return text.strip().startswith("/")

    async def handle(
        self,
        user_id: str,
        chat_id: str,
        message_id: str,
        text: str,
    ) -> bool:
        """处理指令，返回 True 表示已处理（无需转给 AI）。"""
        text = text.strip()
        if not text.startswith("/"):
            return False

        parts = text.split(None, 1)
        cmd   = parts[0].lower()
        args  = parts[1].strip() if len(parts) > 1 else ""

        try:
            if cmd in ("/help", "/h"):
                await self.reply(chat_id, text=HELP_TEXT)

            elif cmd in ("/sh", "/shell"):
                await self._handle_sh(user_id, chat_id, args, force=False)

            elif cmd == "/sh!":
                await self._handle_sh(user_id, chat_id, args, force=True)

            elif cmd in ("/ls", "/dir"):
                await self._handle_ls(chat_id, args)

            elif cmd == "/cat":
                await self._handle_cat(chat_id, args)

            elif cmd == "/rm":
                await self._handle_rm(user_id, chat_id, args)

            elif cmd == "/files":
                await self._handle_files(chat_id)

            elif cmd == "/send":
                await self._handle_send(chat_id, args)

            elif cmd == "/deploy":
                await self._handle_deploy(user_id, chat_id, args)

            elif cmd in ("/runs", "/actions"):
                await self._handle_runs(chat_id, args)

            elif cmd == "/posts":
                await self._handle_posts(chat_id, args)

            elif cmd == "/task":
                await self._handle_task(chat_id, args)

            elif cmd == "/tasks":
                await self._handle_tasks(user_id, chat_id)

            elif cmd == "/status":
                await self._handle_status(user_id, chat_id)

            elif cmd in ("/mem", "/memory"):
                await self._handle_memory(user_id, chat_id, args)

            elif cmd == "/yes":
                await self._handle_confirm(user_id, chat_id)

            else:
                return False  # 未知指令，转给 AI

        except Exception as e:
            log.error("command_error", cmd=cmd, error=str(e))
            await self.reply(chat_id, card=self.card.error(f"指令执行失败", str(e)))

        return True

    # ── Shell ─────────────────────────────────────────────────────────
    async def _handle_sh(self, user_id: str, chat_id: str, cmd: str, force: bool) -> None:
        if not cmd:
            await self.reply(chat_id, text="用法：`/sh <命令>`")
            return

        if self.shell.is_dangerous(cmd) and not force:
            self._pending_dangerous[user_id] = cmd
            await self.reply(
                chat_id,
                text=f"⚠️ 该命令可能有风险：\n```\n{cmd}\n```\n回复 `/yes` 确认执行，或忽略取消。"
            )
            return

        result = await self.shell.run(cmd)
        await self.reply(
            chat_id,
            card=self.card.shell_output(
                command=cmd,
                stdout=result["stdout"],
                returncode=result["returncode"],
                elapsed=result["elapsed"],
                truncated=result["truncated"],
            ),
        )

    async def _handle_confirm(self, user_id: str, chat_id: str) -> None:
        cmd = self._pending_dangerous.pop(user_id, None)
        if not cmd:
            await self.reply(chat_id, text="没有待确认的命令。")
            return
        result = await self.shell.run(cmd)
        await self.reply(
            chat_id,
            card=self.card.shell_output(cmd, result["stdout"],
                                        result["returncode"], result["elapsed"]),
        )

    # ── 文件 ──────────────────────────────────────────────────────────
    async def _handle_ls(self, chat_id: str, path: str) -> None:
        items = self.files.list_dir(path or "")
        if not items:
            await self.reply(chat_id, text=f"目录为空：`{path or '.'}`")
            return
        lines = [f"{'📁' if i['type']=='dir' else '📄'} `{i['name']}` {i['size']/1024:.1f}KB"
                 for i in items[:20]]
        await self.reply(chat_id, text="\n".join(lines))

    async def _handle_cat(self, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/cat <文件路径>`")
            return
        content = self.files.read_file(path)
        await self.reply(chat_id, text=f"```\n{content}\n```")

    async def _handle_rm(self, user_id: str, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/rm <路径>`")
            return
        self._pending_dangerous[user_id] = f"__rm__{path}"
        await self.reply(chat_id, text=f"确认删除 `{path}`？\n回复 `/yes` 确认。")

    async def _handle_files(self, chat_id: str) -> None:
        files = self.bridge.list_stored_files()
        await self.reply(chat_id, card=self.card.file_list(files))

    async def _handle_send(self, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/send <文件路径>`")
            return
        result = await self.bridge.upload_to_lark(path, chat_id)
        await self.reply(chat_id, text=f"✅ 已发送：`{result['file_name']}`")

    # ── GitHub 快捷 ───────────────────────────────────────────────────
    async def _handle_deploy(self, user_id: str, chat_id: str, repo: str) -> None:
        repo = repo or self.hugo_repo
        if not repo:
            await self.reply(chat_id, text="用法：`/deploy <repo>`")
            return
        await self.reply(chat_id, text=f"⏳ 触发 deploy.yml — `{repo}`…")
        result = await self.github.trigger_workflow(repo, "deploy.yml")
        await self.reply(chat_id, text=f"✅ 已触发：{result}")

    async def _handle_runs(self, chat_id: str, repo: str) -> None:
        repo = repo or self.hugo_repo
        if not repo:
            await self.reply(chat_id, text="用法：`/runs <repo>`")
            return
        runs = await self.github.list_workflow_runs(repo, limit=8)
        await self.reply(chat_id, card=self.card.workflow_runs(repo, runs))

    async def _handle_posts(self, chat_id: str, repo: str) -> None:
        repo = repo or self.hugo_repo
        if not repo:
            await self.reply(chat_id, text="用法：`/posts <repo>`")
            return
        posts = await self.github.list_blog_posts(repo)
        await self.reply(chat_id, card=self.card.blog_posts(repo, posts))

    # ── 任务 ──────────────────────────────────────────────────────────
    async def _handle_task(self, chat_id: str, task_id: str) -> None:
        if not task_id:
            await self.reply(chat_id, text="用法：`/task <id>`")
            return
        info = self.memory.get_task(task_id)
        if not info:
            await self.reply(chat_id, text=f"找不到任务 #{task_id}")
            return
        await self.reply(
            chat_id,
            card=self.card.task_status(
                task_id=info["task_id"],
                task_type=info["type"],
                status=info["status"],
                result=info.get("result"),
                error=info.get("error", ""),
            ),
        )

    async def _handle_tasks(self, user_id: str, chat_id: str) -> None:
        tasks = self.memory.get_recent_tasks(user_id, limit=8)
        if not tasks:
            await self.reply(chat_id, text="暂无任务记录。")
            return
        lines = []
        for t in tasks:
            emoji = {"done":"✅","failed":"❌","running":"⏳","pending":"🕐"}.get(t["status"],"❓")
            lines.append(f"{emoji} `#{t['task_id']}` {t['type']} — {t['status']}")
        await self.reply(chat_id, text="**近期任务**\n" + "\n".join(lines))

    # ── 系统状态 ──────────────────────────────────────────────────────
    async def _handle_status(self, user_id: str, chat_id: str) -> None:
        stats  = self.memory.stats()
        tasks  = self.memory.get_recent_tasks(user_id, limit=5)
        disk   = self.files.disk_usage()
        await self.reply(
            chat_id,
            card=self.card.system_status(stats, tasks, disk),
        )

    async def _handle_memory(self, user_id: str, chat_id: str, args: str) -> None:
        if args.strip() == "clear":
            count = self.memory.clear_history(user_id)
            await self.reply(chat_id, text=f"✅ 已清除 {count} 条对话记忆。")
        else:
            history = self.memory.get_history(user_id, limit=5)
            profile = self.memory.get_all_profile(user_id)
            lines = [f"📋 近期 {len(history)} 条对话，用 `/mem clear` 清除。"]
            if profile:
                lines.append("\n**用户画像：**")
                for k, v in profile.items():
                    lines.append(f"- {k}: {v}")
            await self.reply(chat_id, text="\n".join(lines))
