"""
core/memory.py — SQLite 持久化记忆系统
支持：对话历史 / 用户画像 / 任务记录 / KV 知识库
极轻量，适合 e2-micro，无需 Redis/外部服务。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from typing import Any

import structlog

log = structlog.get_logger()


# ─── Schema ──────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- 对话历史（保留最近 N 条）
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL,          -- user | assistant | tool
    content     TEXT NOT NULL,
    model       TEXT,
    tokens      INTEGER DEFAULT 0,
    created_at  REAL DEFAULT (unixepoch('now', 'subsec'))
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, created_at DESC);

-- 用户画像 KV（偏好、习惯、上下文）
CREATE TABLE IF NOT EXISTS user_profile (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  REAL DEFAULT (unixepoch('now', 'subsec')),
    PRIMARY KEY (user_id, key)
);

-- 任务记录
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    type        TEXT NOT NULL,          -- github_action / shell / file / agent
    status      TEXT DEFAULT 'pending', -- pending/running/done/failed
    payload     TEXT,                   -- JSON
    result      TEXT,                   -- JSON
    error       TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at  REAL DEFAULT (unixepoch('now', 'subsec')),
    updated_at  REAL DEFAULT (unixepoch('now', 'subsec'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created_at DESC);

-- 全局 KV（系统级配置、状态）
CREATE TABLE IF NOT EXISTS kv_store (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL DEFAULT (unixepoch('now', 'subsec'))
);

-- GitHub 操作历史
CREATE TABLE IF NOT EXISTS github_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    repo        TEXT,
    action      TEXT,
    detail      TEXT,
    result      TEXT,
    created_at  REAL DEFAULT (unixepoch('now', 'subsec'))
);
"""


@dataclass
class Message:
    user_id: str
    role: str
    content: str
    model: str = ""
    tokens: int = 0


class Memory:
    """线程安全的 SQLite 记忆系统。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()
        log.info("memory_ready", path=db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        """每线程独立连接（WAL 模式支持并发读）。"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10,
            )
            self._local.conn.row_factory = sqlite3.Row
        try:
            yield self._local.conn
            self._local.conn.commit()
        except Exception:
            self._local.conn.rollback()
            raise

    # ── 对话历史 ──────────────────────────────────────────────────
    def add_message(self, msg: Message) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, content, model, tokens) VALUES (?,?,?,?,?)",
                (msg.user_id, msg.role, msg.content, msg.model, msg.tokens),
            )

    def get_history(self, user_id: str, limit: int = 20) -> list[dict]:
        """获取最近 N 条对话，按时间升序（便于拼接到模型 context）。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT role, content, model FROM messages
                   WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_history(self, user_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            return cur.rowcount

    # ── 用户画像 ──────────────────────────────────────────────────
    def set_profile(self, user_id: str, key: str, value: Any) -> None:
        v = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_profile (user_id, key, value, updated_at) VALUES (?,?,?,?)",
                (user_id, key, v, time.time()),
            )

    def get_profile(self, user_id: str, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM user_profile WHERE user_id=? AND key=?",
                (user_id, key),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def get_all_profile(self, user_id: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_profile WHERE user_id=?", (user_id,)
            ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["key"]] = json.loads(r["value"])
            except Exception:
                result[r["key"]] = r["value"]
        return result

    # ── 任务记录 ──────────────────────────────────────────────────
    def create_task(self, task_id: str, user_id: str, task_type: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks (task_id, user_id, type, payload) VALUES (?,?,?,?)",
                (task_id, user_id, task_type, json.dumps(payload, ensure_ascii=False)),
            )

    def update_task(self, task_id: str, status: str,
                    result: dict | None = None, error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE tasks SET status=?, result=?, error=?, updated_at=?
                   WHERE task_id=?""",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result else None,
                    error,
                    time.time(),
                    task_id,
                ),
            )

    def get_task(self, task_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        for f in ("payload", "result"):
            if d.get(f):
                try:
                    d[f] = json.loads(d[f])
                except Exception:
                    pass
        return d

    def get_recent_tasks(self, user_id: str, limit: int = 5) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT task_id, type, status, created_at, updated_at
                   FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── GitHub 历史 ───────────────────────────────────────────────
    def log_github(self, user_id: str, repo: str, action: str,
                   detail: str, result: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO github_history (user_id, repo, action, detail, result) VALUES (?,?,?,?,?)",
                (user_id, repo, action, detail, result),
            )

    # ── KV Store ──────────────────────────────────────────────────
    def kv_set(self, key: str, value: Any) -> None:
        v = json.dumps(value, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?,?,?)",
                (key, v, time.time()),
            )

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    # ── 统计 ──────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._conn() as conn:
            msgs   = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            tasks  = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            users  = conn.execute("SELECT COUNT(DISTINCT user_id) FROM messages").fetchone()[0]
        return {"messages": msgs, "tasks": tasks, "users": users}
