"""
runtime/worker.py — Goal Runtime background workers.

A Worker pulls goal IDs from RuntimeTaskQueue and runs them through
ExecutionEngine. WorkerManager owns lifecycle and health state.

Default worker_count should stay at 1 on e2-micro to avoid git/file/tool
contention.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any

from core.log import get_logger
from runtime.task_queue import RuntimeTaskQueue, RuntimeQueueItem

log = get_logger()


@dataclass
class WorkerState:
    worker_id: str
    running: bool = False
    current_goal_id: str = ""
    processed: int = 0
    failed: int = 0
    last_error: str = ""
    started_at: float | None = None
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeWorker:
    """Single background worker for Goal Runtime."""

    def __init__(self, *, worker_id: str, queue: RuntimeTaskQueue, execution_engine) -> None:
        self.worker_id = worker_id
        self.queue = queue
        self.execution_engine = execution_engine
        self.state = WorkerState(worker_id=worker_id)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self.state.running = True
        self.state.started_at = time.time()
        self.state.updated_at = self.state.started_at
        self._task = asyncio.create_task(self.run(), name=f"runtime-worker-{self.worker_id}")
        log.info("runtime_worker_started", worker_id=self.worker_id)

    async def stop(self) -> None:
        self._stop_event.set()
        self.state.running = False
        self.state.updated_at = time.time()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("runtime_worker_stopped", worker_id=self.worker_id)

    async def run(self) -> None:
        while not self._stop_event.is_set():
            item = await self.queue.get()
            await self._process_item(item)

    async def _process_item(self, item: RuntimeQueueItem) -> None:
        self.state.current_goal_id = item.goal_id
        self.state.updated_at = time.time()
        log.info("runtime_worker_pickup", worker_id=self.worker_id, goal_id=item.goal_id)
        try:
            await self.execution_engine.run_goal(item.goal_id)
            await self.queue.mark_done(item.goal_id)
            self.state.processed += 1
            self.state.last_error = ""
            log.info("runtime_goal_done", worker_id=self.worker_id, goal_id=item.goal_id)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            await self.queue.mark_failed(item.goal_id, error)
            self.state.failed += 1
            self.state.last_error = error
            log.error("runtime_goal_failed", worker_id=self.worker_id, goal_id=item.goal_id, error=error)
        finally:
            self.state.current_goal_id = ""
            self.state.updated_at = time.time()

    def health(self) -> dict[str, Any]:
        return self.state.to_dict()


class WorkerManager:
    """Manage a small pool of RuntimeWorker instances."""

    def __init__(self, *, queue: RuntimeTaskQueue, execution_engine, worker_count: int = 1) -> None:
        self.queue = queue
        self.execution_engine = execution_engine
        self.worker_count = max(1, worker_count)
        self.workers: list[RuntimeWorker] = [
            RuntimeWorker(worker_id=f"worker-{i+1}", queue=queue, execution_engine=execution_engine)
            for i in range(self.worker_count)
        ]

    def start(self) -> None:
        for worker in self.workers:
            worker.start()
        log.info("runtime_worker_manager_started", worker_count=len(self.workers))

    async def stop(self) -> None:
        await asyncio.gather(*(worker.stop() for worker in self.workers), return_exceptions=True)
        log.info("runtime_worker_manager_stopped")

    async def restart(self) -> None:
        await self.stop()
        self.start()

    async def health(self) -> dict[str, Any]:
        queue_snapshot = await self.queue.snapshot()
        return {
            "worker_count": len(self.workers),
            "workers": [worker.health() for worker in self.workers],
            "queue": queue_snapshot,
        }
