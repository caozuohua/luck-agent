"""
core/health.py — 健壮性监控中心
统一处理：
  - 日志回溯（结构化错误日志 → SQLite，支持按时间/级别查询）
  - SQLite 定期 VACUUM + WAL checkpoint
  - _active task 字典清理（防内存泄漏）
  - WS 断线重连通知
  - 系统资源监控（内存/磁盘）
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Coroutine

from core.log import get_logger
from core.redaction import redact_text

log = get_logger()

# ── 日志回溯 Schema ───────────────────────────────────────────────────────────

ERROR_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS error_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT NOT NULL,          -- error | warning | critical
    event       TEXT NOT NULL,          -- structlog event 字段
    detail      TEXT,                   -- 完整错误信息
    user_id     TEXT DEFAULT '',
    source      TEXT DEFAULT '',        -- 模块名
    created_at  REAL DEFAULT (unixepoch('now', 'subsec'))
);
CREATE INDEX IF NOT EXISTS idx_errlog_time  ON error_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_errlog_level ON error_log(level, created_at DESC);
"""


# ── 结构化日志处理器（注入到 structlog pipeline）─────────────────────────────

class DBLogHandler(logging.Handler):
    """
    标准 logging.Handler：把 warning/error 写入 SQLite error_log 表。
    通过 logging.getLogger().addHandler(handler) 注入，无需 structlog。
    """

    def __init__(self, db_path: str) -> None:
        super().__init__(level=logging.WARNING)
        self.db_path = db_path
        self._local  = threading.local()
        self._queue: deque[tuple[str, str, str, str, str, float]] = deque(maxlen=100)
        self._queue_lock = threading.Lock()
        self._flush_event = threading.Event()
        self._closed = False
        self._init()
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="db-log-flusher",
            daemon=True,
        )
        self._writer.start()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=5
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(ERROR_LOG_SCHEMA)

    def emit(self, record: logging.LogRecord) -> None:
        """标准 logging.Handler 接口。"""
        try:
            level  = record.levelname.lower()
            event  = redact_text(record.getMessage())[:200]
            detail = ""
            if record.exc_info:
                import traceback
                detail = redact_text(
                    traceback.format_exception(*record.exc_info)[-1]
                )[:500]
            # 从 extra 字段提取 user_id 和 source
            user_id = redact_text(getattr(record, "user_id", ""))[:50]
            source = redact_text(
                getattr(record, "source", record.module)
            )[:50]
            created_at = time.time()
            with self._queue_lock:
                self._queue.append((level, event, detail, user_id, source, created_at))
                should_flush = len(self._queue) >= 20 or record.levelno >= logging.ERROR
            if should_flush:
                self._flush_event.set()
        except Exception:
            pass   # 日志写入失败绝不影响主流程

    def _writer_loop(self) -> None:
        while not self._closed:
            self._flush_event.wait(3.0)
            self._flush_event.clear()
            self.flush()

    def flush(self) -> None:
        try:
            with self._queue_lock:
                batch = list(self._queue)
                self._queue.clear()
            if batch:
                self._write_many(batch)
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
        self._flush_event.set()
        if getattr(self, "_writer", None) and self._writer.is_alive():
            self._writer.join(timeout=1.0)
        self.flush()
        super().close()

    def _write(self, level: str, event: str, detail: str,
               user_id: str, source: str) -> None:
        self._write_many([(level, event, detail, user_id, source, time.time())])

    def _write_many(self, rows: list[tuple[str, str, str, str, str, float]]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO error_log (level, event, detail, user_id, source, created_at) VALUES (?,?,?,?,?,?)",
                rows,
            )
        # 自动裁剪：保留最近 2000 条
        with self._conn() as conn:
            conn.execute(
                """DELETE FROM error_log WHERE id IN (
                   SELECT id FROM error_log ORDER BY created_at DESC
                   LIMIT -1 OFFSET 2000)"""
            )

    def query(self, level: str = "", hours: int = 24,
              limit: int = 50) -> list[dict]:
        """
        查询最近 N 小时的错误日志。
        level: '' = 全部, 'error' = 仅错误, 'warning' = 仅警告
        """
        self.flush()
        since = time.time() - hours * 3600
        with self._conn() as conn:
            if level:
                rows = conn.execute(
                    """SELECT * FROM error_log
                       WHERE level=? AND created_at>?
                       ORDER BY created_at DESC LIMIT ?""",
                    (level, since, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM error_log
                       WHERE created_at>?
                       ORDER BY created_at DESC LIMIT ?""",
                    (since, limit),
                ).fetchall()
        return [dict(zip(
            ["id", "level", "event", "detail", "user_id", "source", "created_at"], r
        )) for r in rows]

    def stats(self, hours: int = 24) -> dict:
        self.flush()
        since = time.time() - hours * 3600
        with self._conn() as conn:
            total   = conn.execute(
                "SELECT COUNT(*) FROM error_log WHERE created_at>?", (since,)
            ).fetchone()[0]
            errors  = conn.execute(
                "SELECT COUNT(*) FROM error_log WHERE level='error' AND created_at>?",
                (since,)
            ).fetchone()[0]
            warns   = conn.execute(
                "SELECT COUNT(*) FROM error_log WHERE level='warning' AND created_at>?",
                (since,)
            ).fetchone()[0]
        return {"total": total, "errors": errors, "warnings": warns,
                "period_hours": hours}


# ── HealthMonitor ─────────────────────────────────────────────────────────────

class HealthMonitor:
    """
    后台健康监控协程，每隔固定周期执行：
      - SQLite VACUUM（每周一次）
      - WAL checkpoint（每小时）
      - _active task 清理（每10分钟）
      - 内存/磁盘预警（每5分钟）
      - WS 心跳检测（每30秒）
    """

    TICK_FAST   = 30        # WS 心跳
    TICK_MED    = 300       # 资源监控（5min）
    TICK_SLOW   = 600       # task 清理（10min）
    TICK_HOURLY = 1800      # WAL checkpoint（30min，原来1h太长）
    TICK_WEEKLY = 604800    # SQLite VACUUM

    MEM_WARN_MB  = 700      # e2-micro 800MB 上限，700MB 预警
    DISK_WARN_MB = 500      # 磁盘剩余不足 500MB 预警

    def __init__(
        self,
        db_path: str,
        db_log_handler: DBLogHandler,
        task_queue=None,
        notify_fn: Callable[[str], Coroutine] | None = None,
    ) -> None:
        self.db_path     = db_path
        self.db_log      = db_log_handler
        self.task_queue  = task_queue
        self.notify      = notify_fn      # async fn(text) → Lark 通知
        self._running    = False
        self._last_vacuum    = 0.0
        self._last_checkpoint = 0.0
        self._last_cleanup   = 0.0
        self._last_resource  = 0.0
        self._ws_online  = True
        self._ws_last_ok = time.time()

    async def start(self) -> None:
        self._running = True
        # 启动时立即执行一次 WAL checkpoint，合并积压的 WAL 文件
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        await loop.run_in_executor(None, self._wal_checkpoint)
        asyncio.create_task(self._loop(), name="health-monitor")
        log.info("health_monitor_started")

    async def stop(self) -> None:
        self._running = False

    async def _loop(self) -> None:
        while self._running:
            now = time.time()
            try:
                await asyncio.gather(
                    self._check_ws(now),
                    self._check_resources(now),
                    self._cleanup_tasks(now),
                    self._db_maintenance(now),
                )
            except Exception as e:
                log.error("health_monitor_error", error=str(e))
            await asyncio.sleep(self.TICK_FAST)

    # ── WS 心跳 ───────────────────────────────────────────────────────
    def mark_ws_ok(self) -> None:
        """每次收到 Lark 消息时调用，更新心跳时间。"""
        self._ws_last_ok = time.time()
        if not self._ws_online:
            self._ws_online = True
            log.info("ws_reconnected")
            asyncio.create_task(self._notify("✅ WebSocket 已重新连接，Bot 恢复正常。"))

    async def _check_ws(self, now: float) -> None:
        silence = now - self._ws_last_ok
        # 超过5分钟没收到消息，认为可能断线
        if silence > 300 and self._ws_online:
            self._ws_online = False
            log.warning("ws_silence_detected", silence_sec=int(silence))
            await self._notify(
                f"⚠️ Bot 已 {int(silence//60)} 分钟未收到消息，"
                f"WebSocket 可能断线，请检查服务状态。\n"
                f"`sudo systemctl status luck-agent`"
            )

    # ── 资源监控 ──────────────────────────────────────────────────────
    async def _check_resources(self, now: float) -> None:
        if now - self._last_resource < self.TICK_MED:
            return
        self._last_resource = now

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_check_resources)

    def _sync_check_resources(self) -> None:
        import shutil

        # 内存
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        rss_mb = rss_kb / 1024
                        if rss_mb > self.MEM_WARN_MB:
                            log.warning("memory_high",
                                        rss_mb=round(rss_mb, 1),
                                        threshold_mb=self.MEM_WARN_MB)
                            asyncio.get_event_loop().call_soon_threadsafe(
                                lambda: asyncio.create_task(self._notify(
                                    f"⚠️ 内存使用 {rss_mb:.0f}MB，"
                                    f"接近 e2-micro 上限（800MB），注意观察。"
                                ))
                            )
                        break
        except Exception:
            pass

        # 磁盘
        try:
            free_mb = shutil.disk_usage(self.db_path).free / 1024 / 1024
            if free_mb < self.DISK_WARN_MB:
                log.warning("disk_low", free_mb=round(free_mb, 1))
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.create_task(self._notify(
                        f"⚠️ 磁盘剩余 {free_mb:.0f}MB，建议清理。\n"
                        f"`/sh df -h`"
                    ))
                )
        except Exception:
            pass

    # ── Task 字典清理（防内存泄漏）─────────────────────────────────────
    async def _cleanup_tasks(self, now: float) -> None:
        if now - self._last_cleanup < self.TICK_SLOW:
            return
        self._last_cleanup = now

        if not self.task_queue:
            return

        from core.task_queue import TaskStatus
        before = len(self.task_queue._active)

        # 删除完成/失败超过1小时的任务
        cutoff = now - 3600
        stale = [
            tid for tid, t in self.task_queue._active.items()
            if t.status in (TaskStatus.DONE, TaskStatus.FAILED)
            and t.created_at < cutoff
        ]
        for tid in stale:
            del self.task_queue._active[tid]

        if stale:
            log.info("task_cleanup", removed=len(stale), remaining=len(self.task_queue._active))

    # ── SQLite 维护 ───────────────────────────────────────────────────
    async def _db_maintenance(self, now: float) -> None:
        loop = asyncio.get_running_loop()

        # WAL checkpoint（每小时）
        if now - self._last_checkpoint > self.TICK_HOURLY:
            self._last_checkpoint = now
            await loop.run_in_executor(None, self._wal_checkpoint)

        # VACUUM（每周）
        if now - self._last_vacuum > self.TICK_WEEKLY:
            self._last_vacuum = now
            log.info("sqlite_vacuum_start")
            await loop.run_in_executor(None, self._vacuum)
            log.info("sqlite_vacuum_done")

    def _wal_checkpoint(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.close()
        except Exception as e:
            log.warning("wal_checkpoint_failed", error=str(e))

    def _vacuum(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("VACUUM")
            conn.close()
        except Exception as e:
            log.warning("vacuum_failed", error=str(e))

    # ── 通知 ──────────────────────────────────────────────────────────
    async def _notify(self, text: str) -> None:
        if self.notify:
            try:
                await self.notify(text)
            except Exception as e:
                log.error("health_notify_failed", error=str(e))
