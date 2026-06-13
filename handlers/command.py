"""
handlers/command.py — 直接指令处理器(大模型无关)
/cmd, /sh, /file, /git, /task, /status 等指令
即使 Gemini 不可用，这些指令仍然正常工作。
"""
from __future__ import annotations

import asyncio
import shlex
import time
import shutil
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from core.log import get_logger
from core.redaction import redact_text
from core.short_id import short_id
from tools.github_tools import DEFAULT_DEPLOY_WORKFLOW

if TYPE_CHECKING:
    from tools.shell_tools import ShellExecutor, FileManager
    from tools.file_bridge import FileBridge
    from tools.github_tools import GitHubClient
    from core.memory import Memory
    from core.task_queue import TaskQueue
    from cards.builder import CardBuilder

log = get_logger()

AGENT_REPO = "caozuohua/luck-agent"
AGENT_REPO_DIR = Path(__file__).resolve().parents[1]


def _remote_matches_repo(remote_url: str, repo: str) -> bool:
    normalized = remote_url.strip().removesuffix(".git").replace(":", "/")
    return normalized.endswith(f"github.com/{repo}") or normalized.endswith(repo)


HELP_TEXT = """
[直接指令(大模型无关)]

最常用
/status — 运行状态总览（内存/磁盘/进程/任务）
/health — 同 /status（兼容旧入口）
/restart — 重启 luck-agent 服务
/journal [小时数] — systemd 日志回溯
/backup — 备份 SQLite 和记忆配置
/restore <备份名> — 恢复备份
/repair — SQLite checkpoint + vacuum
/upgrade — 拉取远程并重启
/rollback <commit> — 回退到指定提交
/search <关键词> — 搜索（Tavily 优先，自动 fallback）
/pkb <关键词> — 检索个人知识库（Vercel + Supabase）
知识库录入：以 `#` 开头发送消息，支持 `# [question]` / `# [fact] #Topic`

Shell 执行
/sh <命令> — 执行 shell 命令(危险命令需 /yes 确认)
/sh! <命令> — 跳过确认直接执行

文件操作
/ls [路径] — 列出文件区目录
/cat <路径> — 读取文件区文件内容
/rm <路径> — 删除文件(危险路径直接拦截，其他需确认)
/files — 列出已上传文件
/send <路径> — 发送 VPS 文件到 Lark

发布与仓库
/git [路径] [message] — add + commit + push
/deploy [repo] — 触发 deploy-hugo.yml
/runs [repo] — 查看 Actions 运行
/posts [repo] — 列出博文

日志与故障
/logs [error|warning] [小时数] — 查询错误日志
/runtime [goal_id] — Runtime 状态或 Goal 事件链
/task <id> — 查看任务状态
/tasks — 任务列表

记忆管理
/mem — 记忆总览(画像+成功模式+对话，一条消息)
/mem profile|patterns|history — 查看单项
/mem set <key> <value> — 写入画像
/mem del <key|profile|patterns|history> — 删除

定时任务
/schedule list — 查看任务
/schedule add cron|interval <名称> "<cron|秒数>" <prompt> — 新建任务
/schedule pause|resume|cancel <id> — 管理任务

模型切换(对话前缀)
/pro <消息> — 强制 pro
/flash <消息> — 强制 flash
/lite <消息> — 强制 lite

确认
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
        self.health    = None   # 由 agent.py 启动后注入
        self.runtime_observability = None
        from tools.search_tools import SearchTools
        self.searcher  = SearchTools()

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

            elif cmd == "/pkb":
                await self._handle_pkb(chat_id, args)

            elif cmd == "/git":
                await self._handle_git(user_id, chat_id, args)

            elif cmd == "/task":
                await self._handle_task(chat_id, args)

            elif cmd == "/tasks":
                await self._handle_tasks(user_id, chat_id)

            elif cmd == "/status":
                await self._handle_status(user_id, chat_id)

            elif cmd == "/health":
                await self._handle_health(user_id, chat_id)

            elif cmd == "/logs":
                await self._handle_logs(chat_id, args)

            elif cmd == "/runtime":
                await self._handle_runtime(chat_id, args)

            elif cmd == "/restart":
                await self._handle_restart(chat_id)

            elif cmd == "/journal":
                await self._handle_journal(chat_id, args)

            elif cmd == "/backup":
                await self._handle_backup(chat_id)

            elif cmd == "/restore":
                await self._handle_restore(chat_id, args)

            elif cmd == "/repair":
                await self._handle_repair(chat_id)

            elif cmd == "/upgrade":
                await self._handle_upgrade(chat_id)

            elif cmd == "/rollback":
                await self._handle_rollback(chat_id, args)

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
        if result["returncode"] != 0 and result.get("stderr"):
            hint = self.shell.explain_permission_issue(result["stderr"])
            await self.reply(
                chat_id,
                text=f"❌ 执行失败：\n```\n{result['stderr']}\n```\n💡 {hint}",
            )
            return
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
        target = path or "."
        try:
            files = self.files.list_dir(target)
            await self.reply(chat_id, card=self.card.file_list(files, title=f"VPS 文件列表：{target}"))
        except Exception as e:
            await self.reply(chat_id, text=f"❌ `{target}` 不存在或无法访问：{e}")

    async def _handle_cat(self, chat_id: str, path: str) -> None:
        if not path:
            await self.reply(chat_id, text="用法：`/cat <文件路径>`")
            return
        result = self.files.read_file(path)
        if "error" in result:
            await self.reply(chat_id, text=f"❌ {result['error']}")
        else:
            await self.reply(chat_id, text=f"**`{result.get('path', path)}`**\n```\n{result.get('content', '')}\n```")

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
        script = (
            f"cd {shlex.quote(work_dir)} && "
            f"git add -A && "
            f"git commit -m {shlex.quote(message)} && "
            "git push"
        )
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
        await self.reply(chat_id, text=f"⏳ 触发 {DEFAULT_DEPLOY_WORKFLOW} — `{repo}`…")
        result = await self.github.trigger_workflow(repo, DEFAULT_DEPLOY_WORKFLOW)
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
            result = await self.searcher.search(query)
            if "error" in result:
                await self.reply(chat_id, text=f"❌ 搜索失败：{result['error']}")
            else:
                await self.reply(chat_id, card=self.card.search_results(query, result))
        except Exception as e:
            await self.reply(chat_id, text=f"❌ 搜索出错：{e}")

    async def _handle_pkb(self, chat_id: str, query: str) -> None:
        """直接检索个人知识库。"""
        if not query:
            await self.reply(chat_id, text="用法：`/pkb <关键词>`")
            return

        await self.reply(chat_id, text=f"⏳ 检索个人知识库：`{query}`…")
        try:
            from handlers.message import format_pkb_result_items, search_pkb

            result = await search_pkb(query, limit=5)
            if "error" in result:
                await self.reply(chat_id, text=f"❌ 检索失败：{result['error']}")
                return

            items = result.get("results", [])
            if not items:
                await self.reply(chat_id, text=f"📭 未找到与 `{query}` 相关的笔记。")
                return

            card_fn = getattr(self.card, "pkb_results", None)
            if callable(card_fn):
                await self.reply(chat_id, card=card_fn(query, result))
                return

            lines = [f"🗃️ **个人知识库检索**：`{query}`", ""]
            lines.extend(format_pkb_result_items(items, limit=5))

            if result.get("summary"):
                lines.append("")
                lines.append(f"_摘要：{result['summary'][:200]}_")

            await self.reply(chat_id, text="\n".join(lines))
        except Exception as e:
            await self.reply(chat_id, text=f"❌ 检索出错：{e}")

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
        if not info and len(task_id) >= 4:
            matches = self.memory.find_tasks_by_prefix(task_id, limit=2)
            if len(matches) == 1:
                info = matches[0]
            elif len(matches) > 1:
                await self.reply(chat_id, text=f"任务 ID `{task_id}` 不唯一，请多输入几位。")
                return
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
            lines.append(f"{emoji} `#{short_id(t['task_id'])}` {t['type']} — {t['status']}")
        await self.reply(chat_id, text="**近期任务**\n" + "\n".join(lines))

    # ── 系统状态 ──────────────────────────────────────────────────────
    async def _handle_status(self, user_id: str, chat_id: str) -> None:
        stats  = self.memory.stats()
        tasks  = self.memory.get_recent_tasks(user_id, limit=5)
        ws_online = getattr(self.health, "_ws_online", None)
        ws_last_ok = getattr(self.health, "_ws_last_ok", 0.0)
        backup_dir = Path(self.memory.db_path).parent / "backups"
        backup_count = len([p for p in backup_dir.glob("*") if p.is_file()]) if backup_dir.exists() else 0
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
        try:
            from handlers.message import check_pkb_health
            pkb_health = await check_pkb_health()
        except Exception as e:
            pkb_health = {"status": "unknown", "detail": str(e)[:120]}
        extra = {
            "ws_online": ws_online,
            "ws_last_ok": int(time.time() - ws_last_ok) if ws_last_ok else None,
            "db_path": self.memory.db_path,
            "upload_dir": str(self.bridge.storage),
            "shell_work_dir": str(self.shell.work_dir),
            "backup_count": backup_count,
            "backup_dir": str(backup_dir),
            "pkb_status": pkb_health.get("status", "unknown"),
            "pkb_detail": pkb_health.get("detail", ""),
        }
        await self.reply(
            chat_id,
            card=self.card.system_status({**stats, **extra}, tasks, disk, mem, procs),
        )

    async def _handle_health(self, user_id: str, chat_id: str) -> None:
        await self._handle_status(user_id, chat_id)

    async def _handle_restart(self, chat_id: str) -> None:
        await self.reply(chat_id, text="⏳ 重启 luck-agent 服务…")
        result = await self.shell.run(
            "/usr/bin/sudo.ws -n /usr/local/sbin/luck-agent-restart"
        )
        if result["returncode"] == 0:
            await self.reply(chat_id, text="✅ 已重启，服务状态正常。")
        else:
            hint = self.shell.explain_permission_issue(result["stderr"])
            await self.reply(chat_id, text=f"❌ 重启失败：\n```\n{result['stdout']}\n{result['stderr']}\n```\n💡 {hint}")

    async def _handle_journal(self, chat_id: str, args: str) -> None:
        hours = 24
        parts = args.split() if args else []
        for p in parts:
            if p.isdigit():
                hours = min(int(p), 168)
        cmd = f"/usr/bin/sudo.ws -n /usr/local/sbin/luck-agent-journal {hours}"
        result = await self.shell.run(cmd)
        if result["returncode"] == 0:
            output = redact_text(result["stdout"])
            await self.reply(chat_id, text=f"**systemd 日志（最近 {hours}h）**\n```\n{output}\n```")
        else:
            error = redact_text(result["stderr"])
            hint = redact_text(self.shell.explain_permission_issue(error))
            await self.reply(chat_id, text=f"❌ 无法读取 journal：\n```\n{error}\n```\n💡 {hint}")

    async def _handle_runtime(self, chat_id: str, goal_id: str) -> None:
        service = self.runtime_observability
        if service is None:
            await self.reply(chat_id, text="Runtime 诊断服务尚未初始化。")
            return
        try:
            resolver = getattr(service, "resolve_goal_id", None)
            resolved_goal_id = (
                resolver(goal_id)
                if goal_id and callable(resolver)
                else goal_id
            )
            text = (
                await service.goal_timeline(resolved_goal_id)
                if goal_id
                else await service.overview()
            )
        except Exception as error:
            log.error(
                "runtime_observability_failed",
                error_type=type(error).__name__,
            )
            text = "Runtime 诊断查询失败，请查看脱敏后的服务日志。"
        await self.reply(chat_id, text=text)

    async def _handle_backup(self, chat_id: str) -> None:
        db_path = Path(self.memory.db_path)
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        backup_db = backup_dir / f"{db_path.stem}-{stamp}{db_path.suffix}"
        backup_meta = backup_dir / f"{db_path.stem}-{stamp}.meta.txt"

        if not db_path.exists():
            await self.reply(chat_id, text="❌ 数据库不存在，无法备份。")
            return

        shutil.copy2(db_path, backup_db)
        extra_files = []
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, backup_dir / f"{sidecar.name}.{stamp}")
                extra_files.append(sidecar.name)

        backup_meta.write_text(
            "\n".join([
                f"db_path={db_path}",
                f"created_at={stamp} UTC",
                f"extra={','.join(extra_files) if extra_files else 'none'}",
            ]),
            encoding="utf-8",
        )
        await self.reply(chat_id, text=f"✅ 已备份：`{backup_db.name}`")

    async def _handle_restore(self, chat_id: str, args: str) -> None:
        name = args.strip()
        if not name:
            await self._show_backups(chat_id)
            await self.reply(chat_id, text="用法：`/restore <备份名>`")
            return
        db_path = Path(self.memory.db_path)
        backup_dir = db_path.parent / "backups"
        source = backup_dir / name
        if not source.exists():
            await self.reply(chat_id, text=f"❌ 找不到备份：`{name}`")
            return
        shutil.copy2(source, db_path)
        await self.reply(chat_id, text="✅ 已恢复数据库文件。建议立即 `/restart`。")

    async def _show_backups(self, chat_id: str) -> None:
        db_path = Path(self.memory.db_path)
        backup_dir = db_path.parent / "backups"
        if not backup_dir.exists():
            await self.reply(chat_id, text="暂无备份。")
            return
        files = sorted(
            [p for p in backup_dir.iterdir() if p.is_file() and not p.name.endswith(".meta.txt")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            await self.reply(chat_id, text="暂无备份。")
            return
        lines = ["**备份列表**"]
        for p in files[:10]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
            lines.append(f"- `{p.name}` · {round(p.stat().st_size / 1024, 1)} KB · {ts}")
        await self.reply(chat_id, text="\n".join(lines))

    async def _handle_upgrade(self, chat_id: str) -> None:
        repo_dir = str(AGENT_REPO_DIR)
        await self.reply(chat_id, text=f"⏳ 拉取 `{AGENT_REPO}` 并重启…")

        origin = await self.shell.run("git config --get remote.origin.url", cwd=repo_dir)
        if origin["returncode"] != 0:
            await self.reply(chat_id, text=f"❌ 无法确认当前仓库：\n```\n{origin['stdout']}\n{origin['stderr']}\n```")
            return

        remote_url = (origin.get("stdout") or "").strip()
        if not _remote_matches_repo(remote_url, AGENT_REPO):
            await self.reply(
                chat_id,
                text=(
                    f"❌ 当前目录不是 `{AGENT_REPO}`，已停止升级。\n"
                    f"目录：`{repo_dir}`\n"
                    f"origin：`{remote_url or 'unknown'}`"
                ),
            )
            return

        pull = await self.shell.run("git pull --ff-only", cwd=repo_dir)
        if pull["returncode"] != 0:
            await self.reply(chat_id, text=f"❌ 拉取失败：\n```\n{pull['stdout']}\n{pull['stderr']}\n```")
            return

        restart = await self.shell.run("sudo systemctl restart luck-agent", cwd=repo_dir)
        if restart["returncode"] == 0:
            await self.reply(chat_id, text=f"✅ `{AGENT_REPO}` 已更新并重启。\n```\n{pull['stdout']}\n```")
        else:
            hint = self.shell.explain_permission_issue(restart["stderr"])
            await self.reply(
                chat_id,
                text=(
                    f"⚠️ `{AGENT_REPO}` 已拉取，但重启失败：\n"
                    f"```\n{restart['stdout']}\n{restart['stderr']}\n```\n💡 {hint}"
                ),
            )

    async def _handle_rollback(self, chat_id: str, args: str) -> None:
        commit = args.strip()
        if not commit:
            await self.reply(chat_id, text="用法：`/rollback <commit>`")
            return
        await self.reply(chat_id, text=f"⏳ 回退到 `{commit}` 并重启…")
        result = await self.shell.run(f"git checkout {commit} && sudo systemctl restart luck-agent")
        if result["returncode"] == 0:
            await self.reply(chat_id, text=f"```\n{result['stdout']}\n{result['stderr']}\n```")
        else:
            hint = self.shell.explain_permission_issue(result["stderr"])
            await self.reply(chat_id, text=f"```\n{result['stdout']}\n{result['stderr']}\n```\n💡 {hint}")

    async def _handle_repair(self, chat_id: str) -> None:
        db_path = Path(self.memory.db_path)
        if not db_path.exists():
            await self.reply(chat_id, text="❌ 数据库不存在，无法修复。")
            return

        result = await self.shell.run(
            f"python - <<'PY'\n"
            f"import sqlite3\n"
            f"db = r'''{db_path}'''\n"
            f"conn = sqlite3.connect(db, timeout=30)\n"
            f"conn.execute('PRAGMA wal_checkpoint(PASSIVE)')\n"
            f"conn.execute('VACUUM')\n"
            f"conn.close()\n"
            f"print('ok')\n"
            f"PY"
        )
        if result["returncode"] == 0:
            await self.reply(chat_id, text="✅ 已完成 SQLite checkpoint + vacuum。")
        else:
            hint = self.shell.explain_permission_issue(result["stderr"])
            await self.reply(chat_id, text=f"❌ 修复失败：\n```\n{result['stdout']}\n{result['stderr']}\n```\n💡 {hint}")

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
        try:
            parts = shlex.split(args) if args else []
        except ValueError as e:
            await self.reply(chat_id, text=f"❌ 参数解析失败：{e}")
            return
        subcmd = parts[0].lower() if parts else "list"
        sid    = parts[1].strip() if len(parts) > 1 else ""

        if subcmd == "add":
            if len(parts) < 5:
                await self.reply(chat_id, text="用法：`/schedule add cron|interval <名称> \"<cron|秒数>\" <prompt>`")
                return
            mode = sid.lower()
            name = parts[2].strip()
            spec = parts[3].strip()
            prompt = " ".join(parts[4:]).strip()
            if not name or not spec or not prompt:
                await self.reply(chat_id, text="用法：`/schedule add cron|interval <名称> \"<cron|秒数>\" <prompt>`")
                return
            if mode == "cron":
                try:
                    task = self.scheduler.add_cron(user_id, chat_id, name, prompt, spec)
                except ValueError as e:
                    await self.reply(chat_id, text=f"❌ cron 表达式无效：{e}")
                    return
                except Exception as e:
                    await self.reply(chat_id, text=f"❌ 创建 cron 任务失败：{e}")
                    return
            elif mode == "interval":
                if not spec.isdigit():
                    await self.reply(chat_id, text="用法：`/schedule add interval <名称> <秒数> <prompt>`")
                    return
                seconds = int(spec)
                if seconds <= 0:
                    await self.reply(chat_id, text="❌ 间隔秒数必须大于 0。")
                    return
                try:
                    task = self.scheduler.add_interval(user_id, chat_id, name, prompt, seconds)
                except ValueError as e:
                    await self.reply(chat_id, text=f"❌ 间隔任务无效：{e}")
                    return
                except Exception as e:
                    await self.reply(chat_id, text=f"❌ 创建间隔任务失败：{e}")
                    return
            else:
                await self.reply(chat_id, text="用法：`/schedule add cron|interval <名称> \"<cron|秒数>\" <prompt>`")
                return
            card_fn = getattr(self.card, "schedule_created", None)
            if callable(card_fn):
                await self.reply(chat_id, card=card_fn(task.to_dict()))
            else:
                await self.reply(chat_id, text=f"✅ 已创建任务 `#{task.id}`：{task.name}")
            return

        if subcmd == "list" or not subcmd:
            tasks = self.scheduler.list_user(user_id)
            if not tasks:
                await self.reply(chat_id, text="暂无定时任务。用自然语言告诉智能体设置定时任务即可。")
                return
            card_fn = getattr(self.card, "schedule_list", None)
            if callable(card_fn):
                await self.reply(chat_id, card=card_fn([t.to_dict() for t in tasks]))
                return
            lines = ["**📅 定时任务列表**"]
            for t in tasks:
                icon    = "✅" if t.enabled else "⏸"
                if t.mode == "cron":
                    sched = next_cron_desc(t.schedule)
                else:
                    seconds = int(t.schedule)
                    sched = f"每{seconds}秒" if seconds < 60 else f"每{seconds // 60}分钟"
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
            task = self.scheduler.get_for_user(user_id, sid)
            ok = self.scheduler.pause_for_user(user_id, sid)
            card_fn = getattr(self.card, "schedule_action", None)
            if callable(card_fn):
                detail = "已暂停后不会继续触发"
                if task:
                    detail = f"任务名：{task.name} · {detail}"
                task_snapshot = task.to_dict() if task else None
                if task_snapshot is not None:
                    task_snapshot["enabled"] = False
                await self.reply(chat_id, card=card_fn("pause", sid, ok, detail, task_snapshot))
            else:
                await self.reply(chat_id, text=f"{'⏸ 已暂停' if ok else '❌ 找不到任务'} #{sid}")

        elif subcmd == "resume":
            if not sid:
                await self.reply(chat_id, text="用法：`/schedule resume <id>`")
                return
            task = self.scheduler.get_for_user(user_id, sid)
            ok = self.scheduler.resume_for_user(user_id, sid)
            card_fn = getattr(self.card, "schedule_action", None)
            if callable(card_fn):
                detail = "任务已重新加入调度"
                if task:
                    detail = f"任务名：{task.name} · {detail}"
                task_snapshot = task.to_dict() if task else None
                if task_snapshot is not None:
                    task_snapshot["enabled"] = True
                await self.reply(chat_id, card=card_fn("resume", sid, ok, detail, task_snapshot))
            else:
                await self.reply(chat_id, text=f"{'▶️ 已恢复' if ok else '❌ 找不到任务'} #{sid}")

        elif subcmd == "cancel":
            if not sid:
                await self.reply(chat_id, text="用法：`/schedule cancel <id>`")
                return
            task = self.scheduler.get_for_user(user_id, sid)
            ok = self.scheduler.cancel_for_user(user_id, sid)
            card_fn = getattr(self.card, "schedule_action", None)
            if callable(card_fn):
                detail = "任务已从存储中移除"
                if task:
                    detail = f"任务名：{task.name} · {detail}"
                task_snapshot = task.to_dict() if task else None
                if task_snapshot is not None:
                    task_snapshot["enabled"] = False
                    task_snapshot["next_run"] = 0
                await self.reply(chat_id, card=card_fn("cancel", sid, ok, detail, task_snapshot))
            else:
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
