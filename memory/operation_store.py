"""Durable invocation receipts, without raw arguments, tokens or tool output.

`returned_ok` means a tool returned ok; it is never outcome verification.
An interrupted `executing` attempt must be reconciled before any write replay.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from core.operation_context import OperationContext
from core.redaction import redact_text
from memory.db import Database


def fingerprint(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return "invalid-json"
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class OperationAttempt:
    store: OperationStore
    attempt_id: str
    operation_id: str
    started: bool = False

    async def executing(self, *, approved: bool = False) -> None:
        # Set before awaiting: a cancellation could occur after SQLite committed.
        self.started = True
        changed = await self.store.db.execute(
            """UPDATE operation_attempts SET state='executing', approved=?,
                      started_at=?, updated_at=? WHERE attempt_id=? AND state='prepared'""",
            (int(approved), time.time(), time.time(), self.attempt_id),
        )
        if changed != 1:
            raise RuntimeError("attempt is not prepared")

    async def finish(self, *, state: str, error_class: str = "") -> None:
        if state not in {"returned_ok", "returned_error", "rejected", "unknown", "cancelled"}:
            raise ValueError("invalid attempt terminal state")
        changed = await self.store.db.execute(
            """UPDATE operation_attempts SET state=?, error_class=?,
                      finished_at=?, updated_at=? WHERE attempt_id=?
                      AND state IN ('prepared','executing')""",
            (state, redact_text(error_class)[:120], time.time(), time.time(), self.attempt_id),
        )
        if changed != 1:
            raise RuntimeError("attempt is already terminal or missing")


class OperationStore:
    def __init__(self, db: Database, *, deploy_version: str | None = None) -> None:
        self.db = db
        self.deploy_version = redact_text(
            deploy_version or os.environ.get("LUCK_AGENT_RELEASE", "unversioned")
        )[:120]

    async def prepare(
        self, *, context: OperationContext, user_id: str,
        requested_tool: str, tool_name: str, arguments: Any,
        schema: dict | None = None, may_have_effect: bool = True,
    ) -> OperationAttempt:
        attempt_id = uuid.uuid4().hex
        operation_id = context.operation_id or uuid.uuid4().hex
        now = time.time()
        args = arguments if isinstance(arguments, dict) else {}
        # Explicit correlation columns, plus versioned metadata. No tool result,
        # original user request, command, approval token, or exception repr.
        metadata = {
            **asdict(context),
            "operation_id": operation_id,
            "requested_tool": redact_text(requested_tool)[:120],
            "schema_fingerprint": fingerprint(schema or {}),
            "deploy_version": self.deploy_version,
            "target": redact_text(args.get("target", ""))[:160],
            "service": redact_text(args.get("service", ""))[:120],
        }
        await self.db.execute(
            """INSERT INTO operation_attempts
               (attempt_id, operation_id, goal_id, run_id, user_id, tool_name,
                argument_fingerprint, may_have_effect, state, metadata,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)""",
            (attempt_id, operation_id, context.goal_id, context.run_id, user_id,
             redact_text(tool_name)[:120], fingerprint(arguments), int(may_have_effect),
             json.dumps(metadata, ensure_ascii=False), now, now),
        )
        return OperationAttempt(self, attempt_id, operation_id)

    async def list_for_goal(self, goal_id: str) -> list[dict]:
        return [dict(row) for row in await self.db.fetchall(
            "SELECT * FROM operation_attempts WHERE goal_id=? ORDER BY created_at,attempt_id",
            (goal_id,),
        )]

    async def unresolved_writes(self, goal_id: str) -> list[dict]:
        # Even a returned result is not proof that a new graph invocation may
        # replay the effect. Until verification/reconciliation is recorded,
        # all started writes must stop restart replay (including returned_ok).
        return [dict(row) for row in await self.db.fetchall(
            """SELECT * FROM operation_attempts WHERE goal_id=? AND may_have_effect=1
               AND state IN ('executing','unknown','returned_ok','returned_error')
               ORDER BY created_at""", (goal_id,),
        )]
