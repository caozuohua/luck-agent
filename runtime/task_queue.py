"""
runtime/task_queue.py — Lightweight in-process Goal Runtime queue.

This queue intentionally avoids Redis/Celery for e2-micro. It provides bounded
concurrency, FIFO ordering by default, and a simple status snapshot for Lark
commands or debugging.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

QueueStatus = Literal["pending", "running", "done", "failed", "interrupted", "cancelled"]


@dataclass
class RuntimeQueueItem:
    goal_id: str
    user_id: str
    chat_id: str
    priority: int = 100
    status: QueueStatus = "pending"
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueueSettleResult:
    goal: dict[str, Any]
    status: str
    queue_status: QueueStatus
    transitioned: bool


class RuntimeTaskQueue:
    """Small async priority queue for goal execution."""

    def __init__(self, max_active: int = 1) -> None:
        self.max_active = max(1, int(max_active))
        self._queue: asyncio.PriorityQueue[tuple[int, float, str]] = asyncio.PriorityQueue()
        self._items: dict[str, RuntimeQueueItem] = {}
        self._finished_tasks: set[str] = set()
        self._execution_tasks: dict[str, asyncio.Task[Any]] = {}
        self._active_slots: set[str] = set()
        self._slots = asyncio.Semaphore(self.max_active)
        self._lock = asyncio.Lock()

    async def submit(
        self,
        *,
        goal_id: str,
        user_id: str,
        chat_id: str,
        priority: int = 100,
        meta: dict[str, Any] | None = None,
    ) -> RuntimeQueueItem:
        async with self._lock:
            existing = self._items.get(goal_id)
            if existing:
                if existing.status in {"pending", "running"}:
                    return existing
                raise ValueError(f"goal already submitted with terminal status: {goal_id}")
            item = RuntimeQueueItem(
                goal_id=goal_id,
                user_id=user_id,
                chat_id=chat_id,
                priority=priority,
                meta=meta or {},
            )
            self._items[goal_id] = item
            await self._queue.put((priority, item.created_at, goal_id))
        return item

    async def get(self) -> RuntimeQueueItem:
        await self._slots.acquire()
        claimed = False
        try:
            while True:
                _, _, goal_id = await self._queue.get()
                async with self._lock:
                    item = self._items.get(goal_id)
                    if not item or item.status != "pending":
                        self._finish_task(goal_id)
                        continue
                    item.status = "running"
                    item.started_at = time.time()
                    self._active_slots.add(goal_id)
                    claimed = True
                    return item
        finally:
            if not claimed:
                self._slots.release()

    async def get_item(self, goal_id: str) -> RuntimeQueueItem | None:
        async with self._lock:
            return self._items.get(goal_id)

    async def register_execution_task(
        self,
        goal_id: str,
        task: asyncio.Task[Any],
    ) -> bool:
        async with self._lock:
            item = self._items.get(goal_id)
            if item is None or item.status == "cancelled":
                task.cancel()
                return False
            self._execution_tasks[goal_id] = task
            return True

    async def unregister_execution_task(
        self,
        goal_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        async with self._lock:
            if self._execution_tasks.get(goal_id) is task:
                self._execution_tasks.pop(goal_id, None)

    async def mark_done(self, goal_id: str) -> bool:
        return await self._mark_terminal(goal_id, status="done")

    async def mark_failed(self, goal_id: str, error: str) -> bool:
        return await self._mark_terminal(goal_id, status="failed", error=error)

    async def mark_interrupted(self, goal_id: str, reason: str) -> bool:
        return await self._mark_terminal(goal_id, status="interrupted", error=reason)

    async def mark_cancelled(self, goal_id: str, reason: str) -> bool:
        return await self._mark_terminal(
            goal_id,
            status="cancelled",
            error=reason,
            allowed_statuses={"pending", "running", "cancelled"},
        )

    async def cancel(
        self,
        goal_id: str,
        reason: str = "cancelled",
        before_transition: Callable[[RuntimeQueueItem], None] | None = None,
    ) -> bool:
        async with self._lock:
            item = self._items.get(goal_id)
            if not item or item.status not in {"pending", "running"}:
                return False
            if before_transition is not None:
                before_transition(item)
            previous_status = item.status
            item.status = "cancelled"
            item.error = reason
            execution_task = self._execution_tasks.get(goal_id)
            if execution_task is not None:
                execution_task.cancel()
            if previous_status == "pending":
                item.finished_at = time.time()
                self._finish_task(goal_id)
            return True

    async def settle_from_goal(
        self,
        goal_id: str,
        status_provider: Callable[[], dict[str, Any]],
        cancelled_provider: Callable[[str], dict[str, Any]] | None = None,
        *,
        cancelled_is_authoritative: bool = False,
    ) -> QueueSettleResult:
        async with self._lock:
            item = self._items.get(goal_id)
            if item is None:
                goal = status_provider()
                return QueueSettleResult(
                    goal=goal,
                    status=str(goal.get("status") or "failed"),
                    queue_status="failed",
                    transitioned=False,
                )

            authoritative = status_provider()
            transitioned = item.status in {"pending", "running"}
            if item.status == "cancelled":
                authoritative_status = str(
                    authoritative.get("status") or "failed"
                )
                if (
                    not cancelled_is_authoritative
                    and authoritative_status in {"done", "failed", "interrupted"}
                ):
                    goal = authoritative
                    chosen_status = authoritative_status
                    queue_status = (
                        authoritative_status
                        if authoritative_status != "failed"
                        else "failed"
                    )
                    item.status = queue_status
                    item.error = str(authoritative.get("error") or "")
                    transitioned = True
                else:
                    if (
                        authoritative_status != "cancelled"
                        and cancelled_provider is not None
                    ):
                        authoritative = cancelled_provider(
                            item.error or "goal cancelled"
                        )
                    goal = {
                        **authoritative,
                        "status": "cancelled",
                        "error": (
                            item.error
                            or authoritative.get("error")
                            or "goal cancelled"
                        ),
                    }
                    chosen_status = "cancelled"
                    queue_status = "cancelled"
                    transitioned = False
            else:
                goal = authoritative
                chosen_status = str(goal.get("status") or "failed")
                if chosen_status == "done":
                    queue_status = "done"
                    error = ""
                elif chosen_status == "cancelled":
                    queue_status = "cancelled"
                    error = str(goal.get("error") or "goal cancelled")
                elif chosen_status == "interrupted":
                    queue_status = "interrupted"
                    error = str(goal.get("error") or "goal interrupted")
                else:
                    queue_status = "failed"
                    error = str(
                        goal.get("error")
                        or f"goal ended with status {chosen_status}"
                    )
                if transitioned:
                    item.status = queue_status
                    item.error = error

            if goal_id not in self._finished_tasks:
                item.finished_at = time.time()
                self._finish_task(goal_id)
            return QueueSettleResult(
                goal=goal,
                status=chosen_status,
                queue_status=queue_status,
                transitioned=transitioned,
            )

    async def _mark_terminal(
        self,
        goal_id: str,
        *,
        status: QueueStatus,
        error: str = "",
        allowed_statuses: set[QueueStatus] | None = None,
    ) -> bool:
        async with self._lock:
            item = self._items.get(goal_id)
            if (
                not item
                or goal_id in self._finished_tasks
                or item.status not in (allowed_statuses or {"pending", "running"})
            ):
                return False
            item.status = status
            item.error = error
            item.finished_at = time.time()
            self._finish_task(goal_id)
        return True

    def _finish_task(self, goal_id: str) -> None:
        if goal_id in self._finished_tasks:
            return
        self._finished_tasks.add(goal_id)
        if goal_id in self._active_slots:
            self._active_slots.remove(goal_id)
            self._slots.release()
        self._queue.task_done()

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            items = [item.to_dict() for item in self._items.values()]
        counts: dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "counts": counts,
            "items": sorted(items, key=lambda i: i["created_at"], reverse=True),
        }
