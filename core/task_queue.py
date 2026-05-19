"""
core/task_queue.py — 异步任务队列 + 状态机
支持：重试 / 超时 / 进度回调 / 优先级
任务完成后主动推送 Lark 消息卡片。
"""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.log import get_logger

log = get_logger()


class TaskStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    FAILED   = "failed"
    RETRYING = "retrying"


@dataclass
class Task:
    task_id:    str
    user_id:    str
    task_type:  str
    coro_fn:    Callable[..., Coroutine]   # 异步函数
    kwargs:     dict = field(default_factory=dict)
    priority:   int  = 5                   # 1=最高, 10=最低
    max_retry:  int  = 2
    timeout:    int  = 120                 # seconds
    notify_fn:  Callable | None = None    # 完成后回调（发卡片用）

    retry_count: int        = 0
    status:      TaskStatus = TaskStatus.PENDING
    result:      Any        = None
    error:       str        = ""
    created_at:  float      = field(default_factory=time.time)

    def __lt__(self, other: "Task") -> bool:
        return self.priority < other.priority


class TaskQueue:
    """
    asyncio 优先队列 + 多 Worker。
    Worker 数由 Config.TASK_WORKERS 控制，e2-micro 建议 3。
    """

    def __init__(self, workers: int = 3, memory=None) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._workers = workers
        self._memory = memory
        self._active: dict[str, Task] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        for i in range(self._workers):
            asyncio.create_task(self._worker(i), name=f"task-worker-{i}")
        log.info("task_queue_started", workers=self._workers)

    async def stop(self) -> None:
        self._running = False

    async def submit(
        self,
        user_id: str,
        task_type: str,
        coro_fn: Callable,
        kwargs: dict | None = None,
        priority: int = 5,
        max_retry: int = 2,
        timeout: int = 120,
        notify_fn: Callable | None = None,
    ) -> str:
        task_id = str(uuid.uuid4())[:8]
        task = Task(
            task_id=task_id,
            user_id=user_id,
            task_type=task_type,
            coro_fn=coro_fn,
            kwargs=kwargs or {},
            priority=priority,
            max_retry=max_retry,
            timeout=timeout,
            notify_fn=notify_fn,
        )
        self._active[task_id] = task

        # 持久化到 SQLite
        if self._memory:
            self._memory.create_task(task_id, user_id, task_type, kwargs or {})

        await self._queue.put((priority, time.time(), task))
        log.info("task_submitted", task_id=task_id, type=task_type, user_id=user_id[:8])
        return task_id

    def get_task(self, task_id: str) -> Task | None:
        return self._active.get(task_id)

    def get_user_tasks(self, user_id: str, limit: int = 5) -> list[Task]:
        tasks = [t for t in self._active.values() if t.user_id == user_id]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)[:limit]

    async def _worker(self, worker_id: int) -> None:
        log.debug("worker_started", worker_id=worker_id)
        while self._running:
            try:
                _, _, task = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            await self._run_task(task, worker_id)
            self._queue.task_done()

    async def _run_task(self, task: Task, worker_id: int) -> None:
        task.status = TaskStatus.RUNNING
        if self._memory:
            self._memory.update_task(task.task_id, "running")

        log.info("task_running", task_id=task.task_id, type=task.task_type,
                 worker=worker_id, attempt=task.retry_count + 1)

        try:
            result = await asyncio.wait_for(
                task.coro_fn(**task.kwargs),
                timeout=task.timeout,
            )
            task.status = TaskStatus.DONE
            task.result = result

            if self._memory:
                self._memory.update_task(task.task_id, "done", result={"output": str(result)})

            log.info("task_done", task_id=task.task_id, type=task.task_type)

            # 完成回调（发 Lark 卡片）
            if task.notify_fn:
                try:
                    await task.notify_fn(task)
                except Exception as e:
                    log.error("notify_failed", error=str(e))

        except asyncio.TimeoutError:
            await self._handle_failure(task, f"任务超时（{task.timeout}s）")
        except Exception as e:
            await self._handle_failure(task, f"{type(e).__name__}: {e}")

    async def _handle_failure(self, task: Task, error: str) -> None:
        task.error = error
        if task.retry_count < task.max_retry:
            task.retry_count += 1
            task.status = TaskStatus.RETRYING
            delay = 2 ** task.retry_count  # 指数退避
            log.warning("task_retrying", task_id=task.task_id,
                        attempt=task.retry_count, delay=delay, error=error)
            await asyncio.sleep(delay)
            await self._queue.put((task.priority, time.time(), task))
        else:
            task.status = TaskStatus.FAILED
            if self._memory:
                self._memory.update_task(task.task_id, "failed", error=error)
            log.error("task_failed", task_id=task.task_id, error=error)
            if task.notify_fn:
                try:
                    await task.notify_fn(task)
                except Exception:
                    pass
