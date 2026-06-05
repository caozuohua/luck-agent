"""
runtime/task_queue.py — Lightweight in-process Goal Runtime queue.

This queue intentionally avoids Redis/Celery for e2-micro. It provides bounded
concurrency, FIFO ordering by default, and a simple status snapshot for Lark
commands or debugging.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

QueueStatus = Literal["pending", "running", "done", "failed", "cancelled"]


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


class RuntimeTaskQueue:
    """Small async priority queue for goal execution."""

    def __init__(self, max_active: int = 1) -> None:
        self.max_active = max_active
        self._queue: asyncio.PriorityQueue[tuple[int, float, str]] = asyncio.PriorityQueue()
        self._items: dict[str, RuntimeQueueItem] = {}
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
        item = RuntimeQueueItem(
            goal_id=goal_id,
            user_id=user_id,
            chat_id=chat_id,
            priority=priority,
            meta=meta or {},
        )
        async with self._lock:
            self._items[goal_id] = item
            await self._queue.put((priority, item.created_at, goal_id))
        return item

    async def get(self) -> RuntimeQueueItem:
        while True:
            _, _, goal_id = await self._queue.get()
            async with self._lock:
                item = self._items.get(goal_id)
                if not item or item.status != "pending":
                    self._queue.task_done()
                    continue
                item.status = "running"
                item.started_at = time.time()
                return item

    async def mark_done(self, goal_id: str) -> None:
        async with self._lock:
            item = self._items.get(goal_id)
            if item:
                item.status = "done"
                item.finished_at = time.time()
        self._queue.task_done()

    async def mark_failed(self, goal_id: str, error: str) -> None:
        async with self._lock:
            item = self._items.get(goal_id)
            if item:
                item.status = "failed"
                item.error = error
                item.finished_at = time.time()
        self._queue.task_done()

    async def cancel(self, goal_id: str, reason: str = "cancelled") -> bool:
        async with self._lock:
            item = self._items.get(goal_id)
            if not item or item.status not in {"pending", "running"}:
                return False
            item.status = "cancelled"
            item.error = reason
            item.finished_at = time.time()
            return True

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
