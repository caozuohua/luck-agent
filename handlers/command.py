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

**文件操作**
`/ls [路径]` — 列出文件
`/cat <路径>` — 读取文件内容
`/rm <路径>` — 删除文件（需确认）
`/files` — 列出已上传文件
`/send <路径>` — 发送 VPS 文件到 Lark

**Git 快捷**
`/git <路径> [message]` — add + commit + push 指定目录
`/git` — 推送默认项目目录

**GitHub 快捷**
`/deploy [repo]` — 触发 deploy.yml
`/runs [repo]` — 查看 Actions 运行
`/posts [repo]` — 列出博文

**定时任务**
`/schedule list` — 查看所有定时任务
`/schedule pause <id>` — 暂停任务
`/schedule resume <id>` — 恢复任务
`/schedule cancel <id>` — 删除任务

**模型切换（对话前缀）**
`/pro <消息>` — 强制用 gemini-2.5-pro
`/flash <消息>` — 强制用 gemini-2.5-flash
`/lite <消息>` — 强制用 gemini-2.5-flash-lite

**任务管理**
`/task <id>` — 查看任务状态
`/tasks` — 查看我的任务列表

**系统**
`/status` — 系统状态
`/mem [clear]` — 查看/清除对话记忆
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
        self.scheduler = None   # 由 agent.py 启动后注入

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

            elif cmd == "/git":
                await self._handle_git(user_id, chat_id, args)

            elif cmd == "/task":
                await self._handle_task(chat_id, args)

            elif cmd == "/tasks":
                await self._handle_tasks(user_id, chat_id)

            elif cmd == "/status":
                await self._handle_status(user_id, chat_id)

            elif cmd in ("/mem", "/memory"):
                await self._handle_memory(user_id, chat_id, args)

            elif cmd == "/schedule":
                await self._handle_schedule(user_id, chat_id, args)

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
        # 直接走 shell，不受 FileManager 沙箱限制
        target = path or "."
        result = await self.shell.run(f"ls -lah {target}")
        if result["returncode"] != 0:
            await self.reply(chat_id, card=self.card.shell_output(
                f"ls -lah {target}", result["stdout"] or result["stderr"],
                result["returncode"], result["elapsed"]
            ))
        else:
            await self.reply(chat_id, text=f"**`{target}`**\n```\n{result['stdout']}\n```")

    async def _handle_cat(self, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/cat <文件路径>`")
            return
        result = await self.shell.run(f"cat {path}")
        if result["returncode"] != 0:
            await self.reply(chat_id, card=self.card.error(
                f"无法读取文件：{path}", result["stderr"]
            ))
        else:
            await self.reply(chat_id, text=f"```\n{result['stdout']}\n```")

    async def _handle_rm(self, user_id: str, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/rm <路径>`")
            return
        self._pending_dangerous[user_id] = f"rm -rf {path}"
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
    async def _handle_git(self, user_id: str, chat_id: str, args: str) -> None:
        """
        /git [路径] [commit message]
        对指定目录执行 git add -A && commit && push。
        路径和 message 都是可选的，默认路径从 memory 读取或用当前目录。
        """
        # 解析参数：第一个 token 若像路径则作为目录，其余作为 commit message
        parts = args.split(None, 1) if args else []
        if parts and (parts[0].startswith("/") or parts[0].startswith(".")):
            work_dir = parts[0]
            message  = parts[1] if len(parts) > 1 else ""
        else:
            work_dir = self.memory.get_profile(user_id, "default_git_dir",
                                               "/opt/luck-agent")
            message  = args  # 整段作为 message

        if not message:
            # 自动生成 commit message（时间戳）
            from datetime import datetime
            message = f"update {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"

        await self.reply(chat_id, text=f"⏳ 正在推送 `{work_dir}`…")

        # 检查是否有改动
        status = await self.shell.run("git status --porcelain", cwd=work_dir)
        if status["returncode"] != 0:
            await self.reply(chat_id, card=self.card.shell_output(
                "git status", status["stdout"] or status["stderr"],
                status["returncode"], status["elapsed"]
            ))
            return

        if not status["stdout"].strip():
            await self.reply(chat_id, text=f"✅ `{work_dir}` 没有需要提交的改动。")
            return

        # add → commit → push
        script = f"""
cd {work_dir}
git add -A
git commit -m "{message}"
git push
"""
        result = await self.shell.run_script(script)
        if result["returncode"] == 0:
            # 记录常用 git 目录到 memory
            self.memory.set_profile(user_id, "default_git_dir", work_dir)
            await self.reply(chat_id, card=self.card.shell_output(
                f"git push ({work_dir})",
                result["stdout"],
                result["returncode"],
                result["elapsed"],
            ))
        else:
            await self.reply(chat_id, card=self.card.shell_output(
                f"git push ({work_dir})",
                result["stdout"] + "\n" + result["stderr"],
                result["returncode"],
                result["elapsed"],
            ))

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

    async def _handle_schedule(self, user_id: str, chat_id: str, args: str) -> None:
        if not self.scheduler:
            await self.reply(chat_id, text="⚠️ 调度器未初始化。")
            return

        from core.scheduler import next_cron_desc
        parts  = args.split(None, 1)
        subcmd = parts[0].lower() if parts else "list"
        sid    = parts[1].strip() if len(parts) > 1 else ""

        if subcmd == "list" or not subcmd:
            tasks = self.scheduler.list_user(user_id)
            if not tasks:
                await self.reply(chat_id, text="暂无定时任务。用自然语言告诉智能体设置定时任务即可。")
                return
            lines = ["**📅 定时任务列表**"]
            for t in tasks:
                icon    = "✅" if t.enabled else "⏸"
                sched   = next_cron_desc(t.schedule) if t.mode == "cron" \
                          else f"每{int(t.schedule)//60}分钟"
                lines.append(
                    f"{icon} `#{t.id}` **{t.name}**\n"
                    f"   {sched} · 已执行{t.run_count}次\n"
                    f"   _{t.prompt[:50]}{'…' if len(t.prompt)>50 else ''}_"
                )
            await self.reply(chat_id, text="\n\n".join(lines))

        elif subcmd == "pause":
            ok = self.scheduler.pause(sid)
            await self.reply(chat_id, text=f"{'⏸ 已暂停' if ok else '❌ 找不到任务'} #{sid}")

        elif subcmd == "resume":
            ok = self.scheduler.resume(sid)
            await self.reply(chat_id, text=f"{'▶️ 已恢复' if ok else '❌ 找不到任务'} #{sid}")

        elif subcmd == "cancel":
            ok = self.scheduler.cancel(sid)
            await self.reply(chat_id, text=f"{'🗑 已删除' if ok else '❌ 找不到任务'} #{sid}")

        else:
            await self.reply(chat_id, text="用法：`/schedule list|pause|resume|cancel [id]`")

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
