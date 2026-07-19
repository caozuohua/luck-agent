from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from memory.db import Database


class GoalStatus(Enum):
    IDLE = "IDLE"
    ROUTING = "ROUTING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    AWAITING_RESULT = "AWAITING_RESULT"
    EVALUATING = "EVALUATING"
    DONE = "DONE"
    FAILED = "FAILED"


class InvalidGoalTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[GoalStatus, set[GoalStatus]] = {
    GoalStatus.IDLE: {GoalStatus.ROUTING},
    GoalStatus.ROUTING: {GoalStatus.PLANNING, GoalStatus.FAILED},
    GoalStatus.PLANNING: {GoalStatus.EXECUTING, GoalStatus.EVALUATING, GoalStatus.FAILED},
    GoalStatus.EXECUTING: {GoalStatus.AWAITING_RESULT, GoalStatus.FAILED},
    GoalStatus.AWAITING_RESULT: {GoalStatus.EVALUATING, GoalStatus.FAILED},
    GoalStatus.EVALUATING: {GoalStatus.DONE, GoalStatus.FAILED},
    GoalStatus.DONE: set(),
    GoalStatus.FAILED: set(),
}


@dataclass(frozen=True)
class Goal:
    id: str
    user_id: str
    status: GoalStatus
    intent_type: str = ""
    raw_input: str = ""
    plan: str = ""
    tool_calls: str = "[]"
    result: str = ""
    error: str = ""
    retry_count: int = 0
    created_at: int = 0
    updated_at: int = 0


class GoalStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._pending: list[asyncio.Task[None]] = []
        self._last_task: asyncio.Task[None] | None = None

    async def create(self, user_id: str, raw_input: str) -> Goal:
        now = int(time.time())
        goal_id = uuid.uuid4().hex
        await self.db.execute(
            """
            INSERT INTO goals (
                id, user_id, status, intent_type, raw_input, plan, tool_calls,
                result, error, retry_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                goal_id,
                user_id,
                GoalStatus.IDLE.value,
                "",
                raw_input,
                "",
                "[]",
                "",
                "",
                0,
                now,
                now,
            ),
        )
        return Goal(
            id=goal_id,
            user_id=user_id,
            status=GoalStatus.IDLE,
            raw_input=raw_input,
            created_at=now,
            updated_at=now,
        )

    async def update_status(
        self,
        goal_id: str,
        status: GoalStatus,
        **kwargs: Any,
    ) -> None:
        await self._validate_transition(goal_id, status)
        allowed = {
            "intent_type",
            "plan",
            "tool_calls",
            "result",
            "error",
            "retry_count",
        }
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, int(time.time())]
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            if key == "tool_calls" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            params.append(value)
        params.append(goal_id)
        await self.db.execute(
            f"UPDATE goals SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )

    async def _validate_transition(self, goal_id: str, next_status: GoalStatus) -> None:
        row = await self.db.fetchone("SELECT status FROM goals WHERE id = ?", (goal_id,))
        if row is None:
            return
        current = GoalStatus(row["status"])
        if current == next_status:
            return
        if next_status not in ALLOWED_TRANSITIONS[current]:
            raise InvalidGoalTransition(f"invalid goal transition: {current.value} -> {next_status.value}")

    def schedule_status_update(
        self,
        goal_id: str,
        status: GoalStatus,
        **kwargs: Any,
    ) -> asyncio.Task[None]:
        previous = self._last_task

        async def run_after_previous() -> None:
            try:
                if previous is not None:
                    await previous
                await self.update_status(goal_id, status, **kwargs)
            except sqlite3.ProgrammingError as exc:
                # A background status write that fails because the DB is
                # closing (shutdown / test teardown) is non-critical and must
                # not surface as an unretrieved-task exception.
                log.debug("goal_status_update_skipped_db_closed", goal_id=goal_id, error=str(exc))
            except InvalidGoalTransition as exc:
                log.debug("goal_status_update_skipped_invalid", goal_id=goal_id, error=str(exc))

        task = asyncio.create_task(run_after_previous())
        self._last_task = task
        self._pending.append(task)
        task.add_done_callback(lambda done: self._remove_pending(done))
        return task

    async def get_in_progress(self, user_id: str) -> list[Goal]:
        terminal = (GoalStatus.DONE.value, GoalStatus.FAILED.value)
        rows = await self.db.fetchall(
            """
            SELECT * FROM goals
            WHERE user_id = ? AND status NOT IN (?, ?)
            ORDER BY updated_at DESC
            """,
            (user_id, *terminal),
        )
        return [self._row_to_goal(row) for row in rows]

    async def get_recent(self, user_id: str, limit: int = 10) -> list[Goal]:
        rows = await self.db.fetchall(
            "SELECT * FROM goals WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )
        return [self._row_to_goal(row) for row in rows]

    async def drain_pending(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending))

    def _remove_pending(self, task: asyncio.Task[None]) -> None:
        try:
            self._pending.remove(task)
        except ValueError:
            pass

    def _row_to_goal(self, row: Any) -> Goal:
        return Goal(
            id=row["id"],
            user_id=row["user_id"],
            status=GoalStatus(row["status"]),
            intent_type=row["intent_type"] or "",
            raw_input=row["raw_input"] or "",
            plan=row["plan"] or "",
            tool_calls=row["tool_calls"] or "[]",
            result=row["result"] or "",
            error=row["error"] or "",
            retry_count=int(row["retry_count"] or 0),
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
        )
