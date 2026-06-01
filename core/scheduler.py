"""
core/scheduler.py — 持久化定时任务调度器
- 任务存 SQLite，进程重启后自动恢复
- 进程内 asyncio 调度，每分钟检查一次到期任务
- 到期后伪造"用户消息"注入 AgentMessageHandler
- 支持 cron 表达式 和 interval 两种模式
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Coroutine

from core.log import get_logger

log = get_logger()

# ── cron 表达式解析（只实现分/时/日/月/周，够用）─────────────────────────────

def _cron_matches(expr: str, dt: datetime) -> bool:
    """
    判断 datetime 是否匹配 cron 表达式（5字段：分 时 日 月 周）。
    支持：* 、具体值、逗号列表、斜杠步长（*/5）、连字符范围（9-18）。
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    checks = [
        (minute, dt.minute,   0, 59),
        (hour,   dt.hour,     0, 23),
        (dom,    dt.day,      1, 31),
        (month,  dt.month,    1, 12),
        (dow,    dt.weekday(), 0, 6),  # 0=Monday（Python 约定）
    ]
    for field, val, lo, hi in checks:
        if not _field_matches(field, val, lo, hi):
            return False
    return True


def _field_matches(field: str, val: int, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            start = lo if base == "*" else int(base.split("-")[0])
            if val >= start and (val - start) % step == 0:
                return True
        elif "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= val <= int(b):
                return True
        elif int(part) == val:
            return True
    return False


def next_cron_desc(expr: str) -> str:
    """返回 cron 的人类可读描述（简化版）。"""
    parts = expr.split()
    if len(parts) != 5:
        return expr
    m, h, dom, mon, dow = parts
    if m.startswith("*/"):
        return f"每{m[2:]}分钟"
    if h == "*" and dom == "*":
        return f"每天 {h}:{m.zfill(2)}" if h != "*" else f"每小时第{m}分钟"
    if dom == "*" and mon == "*":
        days = {"0":"周一","1":"周二","2":"周三","3":"周四","4":"周五","5":"周六","6":"周日"}
        day_str = days.get(dow, f"周{dow}") if dow != "*" else "每天"
        return f"{day_str} {h}:{m.zfill(2)}"
    return expr


# ── Schema ───────────────────────────────────────────────────────────────────

SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    name        TEXT NOT NULL,           -- 任务名称（给用户看的）
    prompt      TEXT NOT NULL,           -- 触发时注入给 AI 的 prompt
    mode        TEXT NOT NULL,           -- 'cron' | 'interval'
    schedule    TEXT NOT NULL,           -- cron: '0 9 * * 1'  interval: '3600'（秒）
    enabled     INTEGER DEFAULT 1,
    last_run    REAL DEFAULT 0,          -- unix timestamp
    next_run    REAL DEFAULT 0,          -- unix timestamp，interval 模式用
    run_count   INTEGER DEFAULT 0,
    created_at  REAL DEFAULT (unixepoch('now', 'subsec'))
);
"""


# ── ScheduledTask dataclass ───────────────────────────────────────────────────

@dataclass
class ScheduledTask:
    id:       str
    user_id:  str
    chat_id:  str
    name:     str
    prompt:   str
    mode:     str        # 'cron' | 'interval'
    schedule: str
    enabled:  bool
    last_run: float
    next_run: float
    run_count: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "name": self.name,
            "prompt": self.prompt,
            "mode": self.mode,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
        }


# ── ScheduleStore ─────────────────────────────────────────────────────────────

class ScheduleStore:
    """SQLite 持久化层，线程安全。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local  = __import__("threading").local()
        self._init()

    @contextmanager
    def _conn(self):
        if not getattr(self._local, "conn", None):
            self._local.conn = sqlite3.connect(
                self.db_path, check_same_thread=False, timeout=10
            )
            self._local.conn.row_factory = sqlite3.Row
        try:
            yield self._local.conn
            self._local.conn.commit()
        except Exception:
            self._local.conn.rollback()
            raise

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEDULE_SCHEMA)

    def create(self, task: ScheduledTask) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO scheduled_tasks
                   (id, user_id, chat_id, name, prompt, mode, schedule,
                    enabled, last_run, next_run, run_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (task.id, task.user_id, task.chat_id, task.name, task.prompt,
                 task.mode, task.schedule, int(task.enabled),
                 task.last_run, task.next_run, task.run_count),
            )

    def list_all(self) -> list[ScheduledTask]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks ORDER BY created_at"
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_user(self, user_id: str) -> list[ScheduledTask]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE user_id=? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def get(self, task_id: str) -> ScheduledTask | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def update_after_run(self, task_id: str, next_run: float) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE scheduled_tasks
                   SET last_run=?, next_run=?, run_count=run_count+1
                   WHERE id=?""",
                (time.time(), next_run, task_id),
            )

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE scheduled_tasks SET enabled=? WHERE id=?",
                (int(enabled), task_id),
            )
        return cur.rowcount > 0

    def delete(self, task_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM scheduled_tasks WHERE id=?", (task_id,)
            )
        return cur.rowcount > 0

    @staticmethod
    def _row_to_task(row) -> ScheduledTask:
        r = dict(row)
        return ScheduledTask(
            id=r["id"], user_id=r["user_id"], chat_id=r["chat_id"],
            name=r["name"], prompt=r["prompt"], mode=r["mode"],
            schedule=r["schedule"], enabled=bool(r["enabled"]),
            last_run=r["last_run"], next_run=r["next_run"],
            run_count=r["run_count"],
        )


# ── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    """
    asyncio 调度器，每分钟检查一次到期任务。
    到期后调用 trigger_fn(user_id, chat_id, prompt)。
    """

    TICK = 30   # 检查间隔（秒），30s 保证分钟级精度

    def __init__(self, store: ScheduleStore,
                 trigger_fn: Callable[[str, str, str, str], Coroutine]) -> None:
        self._store      = store
        self._trigger    = trigger_fn   # async (task_id, user_id, chat_id, prompt)
        self._running    = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task    = asyncio.create_task(self._loop(), name="scheduler")
        # 恢复 interval 任务的 next_run（进程重启后可能已过期）
        self._recover_interval_tasks()
        log.info("scheduler_started", tick=self.TICK)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    def _recover_interval_tasks(self) -> None:
        """进程重启后，把已过期的 interval 任务的 next_run 重置为"立即运行"。"""
        now = time.time()
        for task in self._store.list_all():
            if task.mode == "interval" and task.enabled and task.next_run < now:
                interval = int(task.schedule)
                self._store.update_after_run(task.id, now + interval)
                log.info("schedule_recovered", id=task.id, name=task.name)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check()
            except Exception as e:
                log.error("scheduler_tick_error", error=str(e))
            await asyncio.sleep(self.TICK)

    async def _check(self) -> None:
        now    = time.time()
        dt_utc = datetime.fromtimestamp(now, tz=timezone.utc)

        for task in self._store.list_all():
            if not task.enabled:
                continue
            if not self._is_due(task, now, dt_utc):
                continue

            log.info("schedule_triggered", id=task.id, name=task.name,
                     user=task.user_id[:8])

            # 计算下次运行时间
            next_run = self._next_run(task, now)
            self._store.update_after_run(task.id, next_run)

            # 异步触发，不阻塞调度器
            asyncio.create_task(
                self._trigger(task.id, task.user_id, task.chat_id, task.prompt),
                name=f"schedule-{task.id}",
            )

    def _is_due(self, task: ScheduledTask, now: float, dt: datetime) -> bool:
        if task.mode == "cron":
            # cron：当前分钟匹配 && 本分钟内未执行过
            minute_start = now - (now % 60)
            return _cron_matches(task.schedule, dt) and task.last_run < minute_start
        else:
            # interval：next_run 已到
            return task.next_run <= now

    def _next_run(self, task: ScheduledTask, now: float) -> float:
        if task.mode == "interval":
            return now + int(task.schedule)
        else:
            # cron：下一个整分钟（调度器会在下次 tick 自然匹配）
            return now + 60

    # ── 对外管理接口 ──────────────────────────────────────────────────
    def add_cron(self, user_id: str, chat_id: str,
                 name: str, prompt: str, cron_expr: str) -> ScheduledTask:
        """添加 cron 定时任务。"""
        task = ScheduledTask(
            id=str(uuid.uuid4())[:8],
            user_id=user_id, chat_id=chat_id,
            name=name, prompt=prompt,
            mode="cron", schedule=cron_expr,
            enabled=True, last_run=0,
            next_run=0, run_count=0,
        )
        self._store.create(task)
        log.info("schedule_added", id=task.id, name=name, mode="cron", expr=cron_expr)
        return task

    def add_interval(self, user_id: str, chat_id: str,
                     name: str, prompt: str, seconds: int) -> ScheduledTask:
        """添加间隔定时任务。"""
        task = ScheduledTask(
            id=str(uuid.uuid4())[:8],
            user_id=user_id, chat_id=chat_id,
            name=name, prompt=prompt,
            mode="interval", schedule=str(seconds),
            enabled=True, last_run=0,
            next_run=time.time() + seconds,
            run_count=0,
        )
        self._store.create(task)
        log.info("schedule_added", id=task.id, name=name,
                 mode="interval", seconds=seconds)
        return task

    def cancel(self, task_id: str) -> bool:
        return self._store.delete(task_id)

    def pause(self, task_id: str) -> bool:
        return self._store.set_enabled(task_id, False)

    def resume(self, task_id: str) -> bool:
        return self._store.set_enabled(task_id, True)

    def list_user(self, user_id: str) -> list[ScheduledTask]:
        return self._store.list_user(user_id)


SCHEDULE_TOOL_SCHEMAS = [
    {
        "name": "schedule_task",
        "description": "创建定时任务。支持 cron 和 interval 两种模式。创建后会在当前用户的 Lark 会话里定时触发 prompt。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "任务名称，便于人类识别"},
                "prompt": {"type": "string", "description": "触发时注入给 AI 的 prompt"},
                "mode": {"type": "string", "enum": ["cron", "interval"], "description": "cron 或 interval"},
                "schedule": {"type": "string", "description": "cron 表达式（5字段）或间隔秒数"},
            },
            "required": ["name", "prompt", "mode", "schedule"],
        },
    },
    {
        "name": "list_schedules",
        "description": "查看当前用户的定时任务列表。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "cancel_schedule",
        "description": "删除指定定时任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "pause_schedule",
        "description": "暂停指定定时任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "resume_schedule",
        "description": "恢复指定定时任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
            },
            "required": ["task_id"],
        },
    },
]
