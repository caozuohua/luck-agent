"""
handlers/command.py — 直接指令处理器(大模型无关)
/cmd, /sh, /file, /git, /task, /status 等指令
即使 Gemini 不可用，这些指令仍然正常工作。
"""
from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.log import get_logger

if TYPE_CHECKING:
    from tools.shell_tools import ShellExecutor, FileManager
    from tools.file_bridge import FileBridge
    from tools.github_tools import GitHubClient
    from core.memory import Memory
    from core.task_queue import TaskQueue
    from cards.builder import CardBuilder

log = get_logger()

HELP_TEXT = """
[直接指令(大模型无关)]

Shell 执行
/sh <命令> — 执行 shell 命令(危险命令需 /yes 确认)
/sh! <命令> — 跳过确认直接执行

文件操作
/ls [路径] — 列出文件(运维友好，支持任意路径)
/cat <路径> — 读取文件内容
/rm <路径> — 删除文件(危险路径直接拦截，其他需确认)
/files — 列出已上传文件
/send <路径> — 发送 VPS 文件到 Lark

Git & GitHub
/git [路径] [message] — add + commit + push
/deploy [repo] — 触发 deploy.yml
/runs [repo] — 查看 Actions 运行
/posts [repo] — 列出博文

系统
/status — 系统状态(内存/磁盘/进程)
/logs [error|warning] [小时数] — 查询错误日志
/search <关键词> — 搜索(Tavily优先,自动fallback到DuckDuckGo/SearXNG/Qwant)

记忆管理
/mem — 记忆总览(画像+成功模式+对话，一条消息)
/mem profile|patterns|history — 查看单项
/mem set <key> <value> — 写入画像
/mem del <key|profile|patterns|history> — 删除

定时任务
/schedule list — 查看任务
/schedule pause|resume|cancel <id> — 管理任务

模型切换(对话前缀)
/pro <消息> — 强制 pro
/flash <消息> — 强制 flash
/lite <消息> — 强制 lite

其他
/task <id> — 查看任务状态
/tasks — 任务列表
/yes — 确认待执行的危险操作
/help — 显示本帮助
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
        self.db_log    = None   # 由 agent.py 启动后注入

        # 待确认的危险命令(防误删)
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
        """处理指令，返回 True 表示已处理(无需转给 AI)。"""
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

            elif cmd == "/search":
                await self._handle_search(user_id, chat_id, args)

            elif cmd == "/git":
                await self._handle_git(user_id, chat_id, args)

            elif cmd == "/task":
                await self._handle_task(chat_id, args)

            elif cmd == "/tasks":
                await self._handle_tasks(user_id, chat_id)

            elif cmd == "/status":
                await self._handle_status(user_id, chat_id)

            elif cmd == "/logs":
                await self._handle_logs(chat_id, args)

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

        if not force and self.shell.is_dangerous(cmd):
            self._pending_dangerous[user_id] = cmd
            await self.reply(
                chat_id,
                text=f"⚠️ 该命令可能有风险：\n```\n{cmd}\n```\n回复 `/yes` 确认执行，或忽略取消。",
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
        # 文件删除走 FileManager(安全路径检查)
        if cmd.startswith("__rm__:"):
            path = cmd[7:]
            result = self.files.delete(path)
            if "error" in result:
                await self.reply(chat_id, text=f"❌ {result['error']}")
            else:
                await self.reply(chat_id, text=f"✅ 已删除：`{path}`")
            return
        # 其他命令走 shell
        result = await self.shell.run(cmd)
        await self.reply(
            chat_id,
            card=self.card.shell_output(cmd, result["stdout"],
                                        result["returncode"], result["elapsed"]),
        )

    # ── 文件 ──────────────────────────────────────────────────────────
    async def _handle_ls(self, chat_id: str, path: str) -> None:
        # 直接走 shell，运维方便，不受 FileManager 沙箱限制
        target = path or "."
        result = await self.shell.run(f"ls -lah {target}")
        if result["returncode"] != 0:
            await self.reply(chat_id, text=f"❌ `{target}` 不存在或无法访问。")
        else:
            await self.reply(chat_id, text=f"**`{target}`**\n```\n{result['stdout']}\n```")

    async def _handle_cat(self, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/cat <文件路径>`")
            return
        result = await self.shell.run(f"cat {path}")
        if result["returncode"] != 0:
            await self.reply(chat_id, text=f"❌ 无法读取 `{path}`：{result['stderr']}")
        else:
            await self.reply(chat_id, text=f"```\n{result['stdout']}\n```")

    async def _handle_rm(self, user_id: str, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/rm <路径>`")
            return
        # 危险路径直接拦截
        if path in (".", "/", "~", "..", "*"):
            await self.reply(chat_id, text=f"❌ 拒绝删除危险路径：`{path}`")
            return
        self._pending_dangerous[user_id] = f"__rm__:{path}"
        await self.reply(chat_id, text=f"确认删除 `{path}`？\n回复 `/yes` 确认。")

    async def _handle_files(self, chat_id: str) -> None:
        if not self.bridge:
            await self.reply(chat_id, text="⚠️ 文件桥接未初始化。")
            return
        files = self.bridge.list_stored_files()
        await self.reply(chat_id, card=self.card.file_list(files))

    async def _handle_send(self, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/send <文件路径>`")
            return
        if not self.bridge:
            await self.reply(chat_id, text="⚠️ 文件桥接未初始化。")
            return
        try:
            result = await self.bridge.upload_to_lark(path, chat_id)
            await self.reply(chat_id, text=f"✅ 已发送：`{result['file_name']}`")
        except Exception as e:
            log.error("send_failed", path=path, chat_id=chat_id, error=str(e)[:300])
            await self.reply(chat_id, text=f"❌ 发送失败：{e}")

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
            # 自动生成 commit message(时间戳)
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

        # add → commit → push(用 run_shell 多行命令，run_script 已删除)
        script = f"cd {work_dir} && git add -A && git commit -m \"{message}\" && git push"
        result = await self.shell.run(script)
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

    async def _handle_search(self, user_id: str, chat_id: str, query: str) -> None:
        """大模型无关的搜索指令。"""
        if not query:
            await self.reply(chat_id, text="用法：`/search <关键词>`")
            return
        await self.reply(chat_id, text=f"⏳ 搜索中：`{query}`…")
        try:
            from tools.search_tools import SearchTools
            searcher = SearchTools()
            result = await searcher.search(query)
            if "error" in result:
                await self.reply(chat_id, text=f"❌ 搜索失败：{result['error']}")
            else:
                formatted = searcher.format_result(result)
                await self.reply(chat_id, text=formatted)
        except Exception as e:
            await self.reply(chat_id, text=f"❌ 搜索出错：{e}")

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
        # 磁盘
        disk_result = await self.shell.run("df -h / | tail -1")
        disk = {"used": "?", "free": "?"}
        if disk_result["returncode"] == 0:
            parts = disk_result["stdout"].split()
            if len(parts) >= 4:
                disk = {"used": parts[2], "free": parts[3]}
        # 内存
        mem_result = await self.shell.run("free -h | awk '/^Mem:/{print $2,$3,$4}'")
        mem = {"total": "?", "used": "?", "free": "?"}
        if mem_result["returncode"] == 0:
            parts = mem_result["stdout"].split()
            if len(parts) >= 3:
                mem = {"total": parts[0], "used": parts[1], "free": parts[2]}
        # 进程数
        ps_result = await self.shell.run("ps aux | wc -l")
        procs = ps_result["stdout"].strip() if ps_result["returncode"] == 0 else "?"
        await self.reply(
            chat_id,
            card=self.card.system_status(stats, tasks, disk, mem, procs),
        )

    async def _handle_logs(self, chat_id: str, args: str) -> None:
        """
        /logs [level] [hours]
        level: error | warning | (空=全部)
        hours: 默认24，最多168(7天)
        """
        if not self.db_log:
            await self.reply(chat_id, text="⚠️ 日志回溯未初始化。")
            return

        parts = args.split() if args else []
        level = ""
        hours = 24

        for p in parts:
            if p in ("error", "warning", "warn"):
                level = "error" if p == "error" else "warning"
            elif p.isdigit():
                hours = min(int(p), 168)

        logs  = self.db_log.query(level=level, hours=hours, limit=30)
        stats = self.db_log.stats(hours=hours)

        if not logs:
            await self.reply(
                chat_id,
                text=f"✅ 最近 {hours}h 无{'错误' if level == 'error' else '警告' if level == 'warning' else '异常'}日志。"
            )
            return

        # 标题行
        lines = [
            f"**📋 日志回溯(最近 {hours}h)**",
            f"错误 {stats['errors']} · 警告 {stats['warnings']}",
            "---",
        ]

        import time as _time
        for entry in logs[:15]:
            ts    = _time.strftime("%m-%d %H:%M", _time.localtime(entry["created_at"]))
            icon  = "❌" if entry["level"] == "error" else "⚠️"
            event = entry["event"][:60]
            detail = entry["detail"][:80] if entry["detail"] else ""
            line  = f"{icon} `{ts}` **{event}**"
            if detail:
                line += f"\n   _{detail}_"
            lines.append(line)

        if len(logs) > 15:
            lines.append(f"\n_…共 {len(logs)} 条，只显示最新 15 条_")

        await self.reply(chat_id, text="\n".join(lines))

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
            if not sid:
                await self.reply(chat_id, text="用法：`/schedule pause <id>`")
                return
            ok = self.scheduler.pause(sid)
            await self.reply(chat_id, text=f"{'⏸ 已暂停' if ok else '❌ 找不到任务'} #{sid}")

        elif subcmd == "resume":
            if not sid:
                await self.reply(chat_id, text="用法：`/schedule resume <id>`")
                return
            ok = self.scheduler.resume(sid)
            await self.reply(chat_id, text=f"{'▶️ 已恢复' if ok else '❌ 找不到任务'} #{sid}")

        elif subcmd == "cancel":
            if not sid:
                await self.reply(chat_id, text="用法：`/schedule cancel <id>`")
                return
            ok = self.scheduler.cancel(sid)
            await self.reply(chat_id, text=f"{'🗑 已删除' if ok else '❌ 找不到任务'} #{sid}")

        else:
            await self.reply(chat_id, text="用法：`/schedule list|pause|resume|cancel [id]`")

    async def _handle_memory(self, user_id: str, chat_id: str, args: str) -> None:
        """
        /mem                   → 完整记忆总览
        /mem profile           → 用户画像
        /mem patterns          → 成功模式列表
        /mem history           → 对话历史摘要
        /mem del <key>         → 删除画像单条
        /mem del profile       → 清空画像
        /mem del patterns      → 清空成功模式
        /mem del history       → 清空对话历史
        /mem set <key> <value> → 写入画像
        """
        parts  = args.strip().split(None, 2) if args.strip() else []
        subcmd = parts[0].lower() if parts else ""

        # ── 删除 ──────────────────────────────────────────────────
        if subcmd == "del":
            target = parts[1].lower() if len(parts) > 1 else ""

            if target == "history":
                count = self.memory.clear_history(user_id)
                await self.reply(chat_id, text=f"✅ 已清空对话历史({count} 条)。")

            elif target == "profile":
                count = self.memory.clear_profile(user_id)
                await self.reply(chat_id, text=f"✅ 已清空用户画像({count} 条)。")

            elif target == "patterns":
                count = self.memory.clear_patterns()
                await self.reply(chat_id, text=f"✅ 已清空成功模式({count} 条)。")

            elif target:
                # 删除画像中的单个 key
                ok = self.memory.delete_profile(user_id, target)
                if ok:
                    await self.reply(chat_id, text=f"✅ 已删除记忆条目：`{target}`")
                else:
                    await self.reply(chat_id, text=f"❌ 找不到记忆条目：`{target}`\n用 `/mem profile` 查看现有条目。")
            else:
                await self.reply(chat_id, text="用法：`/mem del <key|profile|patterns|history>`")
            return

        # ── 写入 ──────────────────────────────────────────────────
        if subcmd == "set":
            if len(parts) < 3:
                await self.reply(chat_id, text="用法：`/mem set <key> <value>`")
                return
            key, value = parts[1], parts[2]
            self.memory.set_profile(user_id, key, value)
            await self.reply(chat_id, text=f"✅ 已写入：`{key}` = `{value}`")
            return

        # ── 查看 ──────────────────────────────────────────────────
        if subcmd == "profile":
            await self._show_profile(user_id, chat_id)
        elif subcmd == "patterns":
            await self._show_patterns(chat_id)
        elif subcmd == "history":
            await self._show_history(user_id, chat_id)
        elif not subcmd:
            # 合并成一条消息
            await self._show_mem_summary(user_id, chat_id)
        else:
            await self.reply(chat_id, text="用法：`/mem [profile|patterns|history|set|del]`")

    async def _show_mem_summary(self, user_id: str, chat_id: str) -> None:
        """无子命令时，合并显示所有记忆摘要。"""
        stats = self.memory.stats()
        profile = self.memory.get_all_profile(user_id)
        INTERNAL = {"default_chat_id", "default_git_dir"}
        visible = {k: v for k, v in profile.items() if k not in INTERNAL}
        patterns = self.memory.get_success_patterns(limit=5)
        history = self.memory.get_history(user_id, limit=3)

        lines = [
            "📊 **记忆统计**",
            f"对话消息 {stats['messages']} 条 · 任务 {stats['tasks']} 条 · 成功模式 {stats['patterns']} 条",
            "",
        ]

        # 画像
        if visible:
            lines.append("📋 **用户画像**")
            for k, v in visible.items():
                lines.append(f"- `{k}`: {v}")
        else:
            lines.append("📋 用户画像：暂无(对话后自动积累)")
        lines.append("")

        # 成功模式
        if patterns:
            lines.append(f"🧠 **成功模式**(前 {len(patterns)} 条)")
            for p in patterns:
                lines.append(f"- `#{p['id']}` [{p['tool']}] {p['intent'][:40]}({p['use_count']}次)")
        else:
            lines.append("🧠 成功模式：暂无")
        lines.append("")

        # 最近对话
        if history:
            lines.append(f"💬 **最近对话**({len(history)} 条)")
            for h in history:
                role = "你" if h["role"] == "user" else "Bot"
                snippet = h["content"][:50] + "…" if len(h["content"]) > 50 else h["content"]
                lines.append(f"- **{role}**：{snippet}")
        lines.append("")
        lines.append("💡 `/mem profile|patterns|history` 查看详情，`/mem set <k> <v>` 写入，`/mem del <key>` 删除。")

        await self.reply(chat_id, text="\n".join(lines))

    async def _show_profile(self, user_id: str, chat_id: str) -> None:
        profile = self.memory.get_all_profile(user_id)
        # 过滤内部系统字段
        INTERNAL = {"default_chat_id", "default_git_dir"}
        visible  = {k: v for k, v in profile.items() if k not in INTERNAL}

        if not visible:
            await self.reply(chat_id, text="📋 **用户画像**\n_暂无内容，和智能体对话后会自动积累。_")
            return

        lines = ["📋 **用户画像**"]
        for k, v in visible.items():
            lines.append(f"- `{k}`: {v}")
        lines.append(f"\n用 `/mem del <key>` 删除单条，`/mem del profile` 清空全部。")
        await self.reply(chat_id, text="\n".join(lines))

    async def _show_patterns(self, chat_id: str) -> None:
        patterns = self.memory.get_success_patterns(limit=20)
        if not patterns:
            await self.reply(chat_id, text="🧠 **成功模式**\n_暂无记录，工具调用成功后自动积累。_")
            return

        lines = [f"🧠 **成功模式**(共 {len(patterns)} 条)"]
        for p in patterns:
            lines.append(
                f"- `#{p['id']}` [{p['tool']}] {p['intent'][:40]}"
                f"(用了{p['use_count']}次)"
            )
        lines.append(f"\n用 `/mem del patterns` 清空全部。")
        await self.reply(chat_id, text="\n".join(lines))

    async def _show_history(self, user_id: str, chat_id: str) -> None:
        history = self.memory.get_history(user_id, limit=8)
        if not history:
            await self.reply(chat_id, text="💬 **对话历史**\n_暂无记录。_")
            return

        lines = [f"💬 **对话历史**(最近 {len(history)} 条)"]
        for h in history:
            role    = "你" if h["role"] == "user" else "Bot"
            snippet = h["content"][:60] + "…" if len(h["content"]) > 60 else h["content"]
            lines.append(f"- **{role}**：{snippet}")
        lines.append(f"\n用 `/mem del history` 清空。")
        await self.reply(chat_id, text="\n".join(lines))
