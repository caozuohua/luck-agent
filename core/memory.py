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

-- 成功工具调用模式（注入到系统 Prompt，提升模型探索意愿）
CREATE TABLE IF NOT EXISTS success_patterns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool        TEXT NOT NULL,           -- 工具名
    intent      TEXT NOT NULL,           -- 用户意图摘要（≤50字）
    command     TEXT NOT NULL,           -- 实际执行的关键参数摘要
    outcome     TEXT NOT NULL,           -- 结果摘要（≤80字）
    use_count   INTEGER DEFAULT 1,       -- 被引用次数，用于排序
    last_used   REAL DEFAULT (unixepoch('now', 'subsec')),
    created_at  REAL DEFAULT (unixepoch('now', 'subsec'))
);
CREATE INDEX IF NOT EXISTS idx_patterns_tool ON success_patterns(tool, use_count DESC);
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

    def delete_profile(self, user_id: str, key: str) -> bool:
        """删除用户画像中的单个 key，返回是否实际删除了。"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM user_profile WHERE user_id=? AND key=?",
                (user_id, key),
            )
        return cur.rowcount > 0

    def clear_profile(self, user_id: str) -> int:
        """清空用户的所有画像条目，返回删除数量。"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM user_profile WHERE user_id=?", (user_id,)
            )
        return cur.rowcount

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

    # ── Success Patterns ──────────────────────────────────────────
    def record_success(self, tool: str, intent: str,
                       command: str, outcome: str) -> None:
        """
        记录一次成功的工具调用。
        相同 tool+intent 组合已存在时，更新 outcome 并累加 use_count，
        避免表无限增长。
        """
        intent  = intent[:50]
        command = command[:120]
        outcome = outcome[:80]

        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM success_patterns WHERE tool=? AND intent=?",
                (tool, intent),
            ).fetchone()

            if row:
                conn.execute(
                    """UPDATE success_patterns
                       SET use_count=use_count+1, outcome=?, command=?, last_used=?
                       WHERE id=?""",
                    (outcome, command, time.time(), row["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO success_patterns
                       (tool, intent, command, outcome) VALUES (?,?,?,?)""",
                    (tool, intent, command, outcome),
                )

    def get_success_patterns(self, limit: int = 12) -> list[dict]:
        """
        返回最常用的成功模式，按 use_count DESC 排序。
        limit 控制注入到 prompt 的条数，避免占用太多 token。
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, tool, intent, command, outcome, use_count
                   FROM success_patterns
                   ORDER BY use_count DESC, last_used DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_pattern(self, pattern_id: int) -> bool:
        """按 id 删除单条成功模式。"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM success_patterns WHERE id=?", (pattern_id,)
            )
        return cur.rowcount > 0

    def clear_patterns(self) -> int:
        """清空所有成功模式，返回删除数量。"""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM success_patterns")
        return cur.rowcount

    # ── 统计 ──────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._conn() as conn:
            msgs     = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            tasks    = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            users    = conn.execute("SELECT COUNT(DISTINCT user_id) FROM messages").fetchone()[0]
            patterns = conn.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
        return {"messages": msgs, "tasks": tasks, "users": users, "patterns": patterns}
