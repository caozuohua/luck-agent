"""
core/memory.py — SQLite 持久化记忆系统
支持：对话历史 / 用户画像 / 任务记录 / Goal Runtime / Lessons / KV 知识库
极轻量，适合 e2-micro，无需 Redis/外部服务。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from core.log import get_logger

log = get_logger()


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

-- Luck-Agent 2.0 Goal Runtime：长期目标状态
CREATE TABLE IF NOT EXISTS goals (
    goal_id          TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    chat_id          TEXT NOT NULL,
    title            TEXT NOT NULL,
    intent           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending', -- pending/running/done/failed/blocked/interrupted/cancelled
    success_criteria TEXT,                            -- JSON array
    current_step     TEXT DEFAULT '',
    plan             TEXT,                            -- JSON object
    artifacts        TEXT,                            -- JSON array
    error            TEXT DEFAULT '',
    created_at       REAL DEFAULT (unixepoch('now', 'subsec')),
    updated_at       REAL DEFAULT (unixepoch('now', 'subsec'))
);
CREATE INDEX IF NOT EXISTS idx_goals_user ON goals(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_goals_intent ON goals(intent, updated_at DESC);

-- Goal Runtime：步骤状态
CREATE TABLE IF NOT EXISTS goal_steps (
    step_id     TEXT PRIMARY KEY,
    goal_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending/running/done/failed/skipped/blocked
    input       TEXT,                            -- JSON object
    output      TEXT,                            -- JSON object
    error       TEXT DEFAULT '',
    retry_count INTEGER DEFAULT 0,
    started_at  REAL,
    finished_at REAL,
    created_at  REAL DEFAULT (unixepoch('now', 'subsec')),
    FOREIGN KEY(goal_id) REFERENCES goals(goal_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goal_steps_goal ON goal_steps(goal_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_goal_steps_status ON goal_steps(status, created_at DESC);

-- Lessons Learned：失败经验 / 修复经验
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT NOT NULL,
    task_type     TEXT NOT NULL,
    error_pattern TEXT NOT NULL,
    root_cause    TEXT DEFAULT '',
    solution      TEXT NOT NULL,
    prevention    TEXT DEFAULT '',
    confidence    REAL DEFAULT 0.5,
    use_count     INTEGER DEFAULT 0,
    created_at    REAL DEFAULT (unixepoch('now', 'subsec')),
    updated_at    REAL DEFAULT (unixepoch('now', 'subsec')),
    UNIQUE(domain, task_type, error_pattern)
);
CREATE INDEX IF NOT EXISTS idx_lessons_domain_task ON lessons(domain, task_type, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_lessons_error ON lessons(error_pattern);

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

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _json_loads(value: Any, default: Any = None) -> Any:
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except Exception:
            return value

    @staticmethod
    def _decode_json_fields(row: sqlite3.Row | dict, fields: tuple[str, ...]) -> dict:
        d = dict(row)
        for field in fields:
            d[field] = Memory._json_loads(d.get(field), [] if field in {"success_criteria", "artifacts"} else {})
        return d

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
        v = self._json_dumps(value) if not isinstance(value, str) else value
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
        return self._json_loads(row["value"], default)

    def get_all_profile(self, user_id: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_profile WHERE user_id=?", (user_id,)
            ).fetchall()
        result = {}
        for r in rows:
            result[r["key"]] = self._json_loads(r["value"], r["value"])
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
                "DELETE FROM user_profile WHERE user_id=?",
                (user_id,),
            )
        return cur.rowcount

    # ── 任务记录 ──────────────────────────────────────────────────
    def create_task(self, task_id: str, user_id: str, task_type: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks (task_id, user_id, type, payload) VALUES (?,?,?,?)",
                (task_id, user_id, task_type, self._json_dumps(payload)),
            )

    def update_task(self, task_id: str, status: str,
                    result: dict | None = None, error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE tasks SET status=?, result=?, error=?, updated_at=?
                   WHERE task_id=?""",
                (
                    status,
                    self._json_dumps(result) if result else None,
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
        return self._decode_json_fields(row, ("payload", "result"))

    def get_recent_tasks(self, user_id: str, limit: int = 5) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT task_id, type, status, created_at, updated_at
                   FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Goal Runtime ───────────────────────────────────────────────
    def create_goal(self, goal: dict) -> None:
        """Create or replace a long-running Goal Runtime record."""
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO goals
                   (goal_id, user_id, chat_id, title, intent, status,
                    success_criteria, current_step, plan, artifacts, error,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    goal["goal_id"],
                    goal["user_id"],
                    goal["chat_id"],
                    goal["title"],
                    goal["intent"],
                    goal.get("status", "pending"),
                    self._json_dumps(goal.get("success_criteria", [])),
                    goal.get("current_step", ""),
                    self._json_dumps(goal.get("plan", {})),
                    self._json_dumps(goal.get("artifacts", [])),
                    goal.get("error", ""),
                    goal.get("created_at", now),
                    goal.get("updated_at", now),
                ),
            )

    def get_goal(self, goal_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
        if not row:
            return None
        return self._decode_json_fields(row, ("success_criteria", "plan", "artifacts"))

    def update_goal(self, goal_id: str, **updates: Any) -> bool:
        """Update mutable goal fields. JSON fields accept native Python objects."""
        allowed = {
            "title", "intent", "status", "success_criteria", "current_step",
            "plan", "artifacts", "error",
        }
        values: dict[str, Any] = {k: v for k, v in updates.items() if k in allowed}
        if not values:
            return False

        for field in ("success_criteria", "plan", "artifacts"):
            if field in values:
                values[field] = self._json_dumps(values[field])
        values["updated_at"] = time.time()

        set_clause = ", ".join(f"{k}=?" for k in values)
        params = list(values.values()) + [goal_id]
        with self._conn() as conn:
            cur = conn.execute(f"UPDATE goals SET {set_clause} WHERE goal_id=?", params)
        return cur.rowcount > 0

    def list_goals(self, user_id: str | None = None, status: str | None = None,
                   limit: int = 20) -> list[dict]:
        clauses, params = [], []
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT * FROM goals {where}
                    ORDER BY updated_at DESC LIMIT ?""",
                params,
            ).fetchall()
        return [self._decode_json_fields(r, ("success_criteria", "plan", "artifacts")) for r in rows]

    def create_goal_step(self, step: dict) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO goal_steps
                   (step_id, goal_id, name, status, input, output, error,
                    retry_count, started_at, finished_at, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    step["step_id"],
                    step["goal_id"],
                    step["name"],
                    step.get("status", "pending"),
                    self._json_dumps(step.get("input", {})),
                    self._json_dumps(step.get("output", {})),
                    step.get("error", ""),
                    int(step.get("retry_count", 0)),
                    step.get("started_at"),
                    step.get("finished_at"),
                    step.get("created_at", now),
                ),
            )

    def update_goal_step(self, step_id: str, **updates: Any) -> bool:
        allowed = {
            "name", "status", "input", "output", "error",
            "retry_count", "started_at", "finished_at",
        }
        values: dict[str, Any] = {k: v for k, v in updates.items() if k in allowed}
        if not values:
            return False
        for field in ("input", "output"):
            if field in values:
                values[field] = self._json_dumps(values[field])
        set_clause = ", ".join(f"{k}=?" for k in values)
        params = list(values.values()) + [step_id]
        with self._conn() as conn:
            cur = conn.execute(f"UPDATE goal_steps SET {set_clause} WHERE step_id=?", params)
        return cur.rowcount > 0

    def get_goal_step(self, step_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM goal_steps WHERE step_id=?", (step_id,)).fetchone()
        if not row:
            return None
        return self._decode_json_fields(row, ("input", "output"))

    def get_goal_steps(self, goal_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM goal_steps WHERE goal_id=? ORDER BY created_at ASC",
                (goal_id,),
            ).fetchall()
        return [self._decode_json_fields(r, ("input", "output")) for r in rows]

    def delete_goal(self, goal_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM goals WHERE goal_id=?", (goal_id,))
        return cur.rowcount > 0

    # ── Lessons Learned ───────────────────────────────────────────
    def save_lesson(self, lesson: dict) -> int:
        """Insert or update a lesson by domain/task_type/error_pattern."""
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO lessons
                   (domain, task_type, error_pattern, root_cause, solution,
                    prevention, confidence, use_count, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(domain, task_type, error_pattern) DO UPDATE SET
                       root_cause=excluded.root_cause,
                       solution=excluded.solution,
                       prevention=excluded.prevention,
                       confidence=excluded.confidence,
                       updated_at=excluded.updated_at""",
                (
                    lesson["domain"],
                    lesson["task_type"],
                    lesson["error_pattern"],
                    lesson.get("root_cause", ""),
                    lesson["solution"],
                    lesson.get("prevention", ""),
                    float(lesson.get("confidence", 0.5)),
                    int(lesson.get("use_count", 0)),
                    lesson.get("created_at", now),
                    lesson.get("updated_at", now),
                ),
            )
            row = conn.execute(
                """SELECT lesson_id FROM lessons
                   WHERE domain=? AND task_type=? AND error_pattern=?""",
                (lesson["domain"], lesson["task_type"], lesson["error_pattern"]),
            ).fetchone()
        return int(row["lesson_id"]) if row else 0

    def get_lesson(self, lesson_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM lessons WHERE lesson_id=?", (lesson_id,)).fetchone()
        return dict(row) if row else None

    def search_lessons(self, domain: str | None = None, task_type: str | None = None,
                       query: str | None = None, limit: int = 10) -> list[dict]:
        clauses, params = [], []
        if domain:
            clauses.append("domain=?")
            params.append(domain)
        if task_type:
            clauses.append("task_type=?")
            params.append(task_type)
        if query:
            like = f"%{query}%"
            clauses.append("(error_pattern LIKE ? OR root_cause LIKE ? OR solution LIKE ? OR prevention LIKE ?)")
            params.extend([like, like, like, like])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT * FROM lessons {where}
                    ORDER BY confidence DESC, use_count DESC, updated_at DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_lesson_used(self, lesson_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE lessons
                   SET use_count=use_count+1, updated_at=?
                   WHERE lesson_id=?""",
                (time.time(), lesson_id),
            )
        return cur.rowcount > 0

    def delete_lesson(self, lesson_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM lessons WHERE lesson_id=?", (lesson_id,))
        return cur.rowcount > 0

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
        v = self._json_dumps(value)
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
        return self._json_loads(row["value"], default)

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
            msgs       = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            tasks      = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            users      = conn.execute("SELECT COUNT(DISTINCT user_id) FROM messages").fetchone()[0]
            patterns   = conn.execute("SELECT COUNT(*) FROM success_patterns").fetchone()[0]
            goals      = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
            goal_steps = conn.execute("SELECT COUNT(*) FROM goal_steps").fetchone()[0]
            lessons    = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        return {
            "messages": msgs,
            "tasks": tasks,
            "users": users,
            "patterns": patterns,
            "goals": goals,
            "goal_steps": goal_steps,
            "lessons": lessons,
        }
