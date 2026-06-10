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
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from core.log import get_logger
from runtime.task_queue import RuntimeTaskQueue, RuntimeQueueItem

log = get_logger()

TerminalCallback = Callable[[dict[str, Any]], Awaitable[None]]


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

    def __init__(
        self,
        *,
        worker_id: str,
        queue: RuntimeTaskQueue,
        execution_engine,
        terminal_callback: TerminalCallback | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.queue = queue
        self.execution_engine = execution_engine
        self.terminal_callback = terminal_callback
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
        queue_settled = False
        goal: dict[str, Any] | None = None
        try:
            try:
                goal = await self.execution_engine.run_goal(item.goal_id)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                try:
                    goal = self.execution_engine.goal_manager.fail_goal(item.goal_id, error)
                except Exception as persist_error:
                    log.error(
                        "runtime_goal_persist_failed",
                        goal_id=item.goal_id,
                        error=f"{type(persist_error).__name__}: {persist_error}",
                        original_error=error,
                    )
                    goal = {
                        "goal_id": item.goal_id,
                        "user_id": item.user_id,
                        "chat_id": item.chat_id,
                        "status": "failed",
                        "error": error,
                        "artifacts": [],
                    }

            goal = await self._preserve_queue_cancellation(item, goal)
            status, terminal_error, queue_settled = await self._account_terminal_goal(item, goal)
            if queue_settled:
                await self._notify_terminal_goal(item, goal, status)
                self._log_terminal_goal(item, status, terminal_error)
        except asyncio.CancelledError:
            if not queue_settled:
                goal = goal or self._persist_interrupted_goal(item)
                status, terminal_error, queue_settled = await self._account_terminal_goal(item, goal)
                if queue_settled:
                    await self._notify_terminal_goal(item, goal, status)
                    self._log_terminal_goal(item, status, terminal_error)
            raise
        finally:
            self.state.current_goal_id = ""
            self.state.updated_at = time.time()

    async def _account_terminal_goal(
        self,
        item: RuntimeQueueItem,
        goal: dict[str, Any],
    ) -> tuple[str, str, bool]:
        status = str(goal.get("status") or "failed")
        if status == "done":
            transitioned = await self.queue.mark_done(item.goal_id)
            if transitioned:
                self.state.processed += 1
                self.state.last_error = ""
            return status, "", transitioned
        if status == "cancelled":
            terminal_error = str(goal.get("error") or "goal cancelled")
            transitioned = await self.queue.mark_cancelled(item.goal_id, terminal_error)
            if transitioned:
                self.state.last_error = terminal_error
            return status, terminal_error, transitioned
        if status == "interrupted":
            terminal_error = str(goal.get("error") or "goal interrupted")
            transitioned = await self.queue.mark_interrupted(item.goal_id, terminal_error)
            if transitioned:
                self.state.last_error = terminal_error
            return status, terminal_error, transitioned

        terminal_error = str(goal.get("error") or f"goal ended with status {status}")
        transitioned = await self.queue.mark_failed(item.goal_id, terminal_error)
        if transitioned:
            self.state.failed += 1
            self.state.last_error = terminal_error
        return status, terminal_error, transitioned

    async def _preserve_queue_cancellation(
        self,
        item: RuntimeQueueItem,
        goal: dict[str, Any],
    ) -> dict[str, Any]:
        queue_item = await self.queue.get_item(item.goal_id)
        if not queue_item or queue_item.status != "cancelled":
            return goal
        return {
            **goal,
            "goal_id": item.goal_id,
            "user_id": goal.get("user_id") or item.user_id,
            "chat_id": goal.get("chat_id") or item.chat_id,
            "status": "cancelled",
            "error": queue_item.error or "goal cancelled",
            "artifacts": goal.get("artifacts") or [],
        }

    async def _notify_terminal_goal(
        self,
        item: RuntimeQueueItem,
        goal: dict[str, Any],
        status: str,
    ) -> None:
        if not self.terminal_callback or status not in {"done", "blocked", "failed"}:
            return
        try:
            await self.terminal_callback(goal)
        except Exception as notify_error:
            log.error(
                "runtime_terminal_notify_failed",
                goal_id=item.goal_id,
                status=status,
                error=str(notify_error),
            )

    def _log_terminal_goal(
        self,
        item: RuntimeQueueItem,
        status: str,
        terminal_error: str,
    ) -> None:
        if status == "done":
            log.info("runtime_goal_done", worker_id=self.worker_id, goal_id=item.goal_id)
        elif status in {"cancelled", "interrupted"}:
            log.info(
                f"runtime_goal_{status}",
                worker_id=self.worker_id,
                goal_id=item.goal_id,
                error=terminal_error,
            )
        else:
            log.error(
                "runtime_goal_failed",
                worker_id=self.worker_id,
                goal_id=item.goal_id,
                status=status,
                error=terminal_error,
            )

    def _persist_interrupted_goal(self, item: RuntimeQueueItem) -> dict[str, Any]:
        reason = "worker stopped"
        goal_manager = self.execution_engine.goal_manager
        pause_goal = getattr(goal_manager, "pause_goal", None)
        if pause_goal:
            try:
                return pause_goal(item.goal_id, reason=reason)
            except Exception:
                get_goal = getattr(goal_manager, "get_goal", None)
                if get_goal:
                    try:
                        goal = get_goal(item.goal_id)
                        if goal.get("status") == "cancelled":
                            return goal
                    except Exception:
                        pass
        try:
            return goal_manager.fail_goal(item.goal_id, reason)
        except Exception as persist_error:
            log.error(
                "runtime_goal_persist_failed",
                goal_id=item.goal_id,
                error=f"{type(persist_error).__name__}: {persist_error}",
                original_error=reason,
            )
            return {
                "goal_id": item.goal_id,
                "user_id": item.user_id,
                "chat_id": item.chat_id,
                "status": "failed",
                "error": reason,
                "artifacts": [],
            }

    def health(self) -> dict[str, Any]:
        return self.state.to_dict()


class WorkerManager:
    """Manage a small pool of RuntimeWorker instances."""

    def __init__(
        self,
        *,
        queue: RuntimeTaskQueue,
        execution_engine,
        worker_count: int = 1,
        terminal_callback: TerminalCallback | None = None,
    ) -> None:
        self.queue = queue
        self.execution_engine = execution_engine
        self.worker_count = max(1, worker_count)
        self.workers: list[RuntimeWorker] = [
            RuntimeWorker(
                worker_id=f"worker-{i+1}",
                queue=queue,
                execution_engine=execution_engine,
                terminal_callback=terminal_callback,
            )
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
