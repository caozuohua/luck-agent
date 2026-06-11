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
from runtime.events import NoopRuntimeEventRecorder
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
        event_recorder=None,
        notification_timeout: float = 30.0,
        notification_stop_timeout: float | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.queue = queue
        self.execution_engine = execution_engine
        self.terminal_callback = terminal_callback
        self.event_recorder = (
            event_recorder
            if event_recorder is not None
            else NoopRuntimeEventRecorder()
        )
        self.notification_timeout = notification_timeout
        self.notification_stop_timeout = (
            notification_timeout + 0.1
            if notification_stop_timeout is None
            else notification_stop_timeout
        )
        self.state = WorkerState(worker_id=worker_id)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping_task: asyncio.Task[None] | None = None
        self._notification_tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping_task = None
        self._stop_event.clear()
        self.state.running = True
        self.state.started_at = time.time()
        self.state.updated_at = self.state.started_at
        self._task = asyncio.create_task(self.run(), name=f"runtime-worker-{self.worker_id}")
        log.info("runtime_worker_started", worker_id=self.worker_id)

    async def stop(self) -> None:
        if self._stopping_task is None:
            self._stopping_task = asyncio.create_task(
                self._stop_once(),
                name=f"runtime-worker-stop-{self.worker_id}",
            )
        await asyncio.shield(self._stopping_task)

    async def _stop_once(self) -> None:
        self._stop_event.set()
        self.state.running = False
        self.state.updated_at = time.time()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._stop_notifications()
        log.info("runtime_worker_stopped", worker_id=self.worker_id)

    async def run(self) -> None:
        while not self._stop_event.is_set():
            item = await self.queue.get()
            await self._process_item(item)

    async def _process_item(self, item: RuntimeQueueItem) -> None:
        self.state.current_goal_id = item.goal_id
        self.state.updated_at = time.time()
        log.info("runtime_worker_pickup", worker_id=self.worker_id, goal_id=item.goal_id)
        self._record(
            "worker.pickup",
            item,
            status="running",
            payload={"worker_id": self.worker_id},
        )
        queue_settled = False
        goal: dict[str, Any] | None = None
        status = ""
        terminal_error = ""
        try:
            try:
                goal = await self.execution_engine.run_goal(item.goal_id)
            except Exception as exc:
                error = f"execution failed: {type(exc).__name__}"
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

            goal, status, terminal_error, queue_settled = (
                await self._settle_authoritative_goal(item, goal)
            )
            if queue_settled:
                self._start_terminal_notification(item, goal, status)
                self._log_terminal_goal(item, status, terminal_error)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            goal = await self._goal_after_worker_cancel(item, goal)
            if not queue_settled:
                goal, status, terminal_error, queue_settled = (
                    await self._settle_authoritative_goal(item, goal)
                )
                if queue_settled:
                    self._start_terminal_notification(item, goal, status)
                    self._log_terminal_goal(item, status, terminal_error)
            raise
        finally:
            self.state.current_goal_id = ""
            self.state.updated_at = time.time()

    async def _settle_authoritative_goal(
        self,
        item: RuntimeQueueItem,
        goal: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str, bool]:
        goal_manager = self.execution_engine.goal_manager
        get_goal = getattr(goal_manager, "get_goal", None)

        def status_provider() -> dict[str, Any]:
            if get_goal is None:
                return goal
            return get_goal(item.goal_id)

        settle = getattr(self.queue, "settle_from_goal", None)
        if settle is None:
            authoritative = status_provider()
            status, error, transitioned = await self._account_terminal_goal(
                item,
                authoritative,
            )
            return authoritative, status, error, transitioned

        cancel_goal = getattr(goal_manager, "cancel_goal", None)

        def cancelled_provider(reason: str) -> dict[str, Any]:
            if cancel_goal is None:
                return {
                    **goal,
                    "status": "cancelled",
                    "error": reason,
                }
            return cancel_goal(item.goal_id, reason)

        result = await settle(
            item.goal_id,
            status_provider,
            cancelled_provider,
            cancelled_is_authoritative=get_goal is None,
        )
        authoritative = result.goal
        status = result.status
        terminal_error = str(authoritative.get("error") or "")
        if status == "done":
            terminal_error = ""
            if result.transitioned:
                self.state.processed += 1
                self.state.last_error = ""
                self._record("queue.completed", item, status="done")
        elif status == "cancelled":
            terminal_error = terminal_error or "goal cancelled"
            self.state.last_error = terminal_error
            if result.transitioned:
                self._record("queue.cancelled", item, status="cancelled")
        elif status == "interrupted":
            terminal_error = terminal_error or "goal interrupted"
            self.state.last_error = terminal_error
            if result.transitioned:
                self._record("worker.interrupted", item, status="interrupted")
        else:
            terminal_error = terminal_error or f"goal ended with status {status}"
            if result.transitioned:
                self.state.failed += 1
                self.state.last_error = terminal_error
                self._record(
                    "queue.failed",
                    item,
                    status="failed",
                    payload={"goal_status": status},
                )
        return authoritative, status, terminal_error, result.transitioned

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
                self._record("queue.completed", item, status="done")
            return status, "", transitioned
        if status == "cancelled":
            terminal_error = str(goal.get("error") or "goal cancelled")
            transitioned = await self.queue.mark_cancelled(item.goal_id, terminal_error)
            if transitioned:
                self.state.last_error = terminal_error
                self._record("queue.cancelled", item, status="cancelled")
            return status, terminal_error, transitioned
        if status == "interrupted":
            terminal_error = str(goal.get("error") or "goal interrupted")
            transitioned = await self.queue.mark_interrupted(item.goal_id, terminal_error)
            if transitioned:
                self.state.last_error = terminal_error
                self._record("worker.interrupted", item, status="interrupted")
            return status, terminal_error, transitioned

        terminal_error = str(goal.get("error") or f"goal ended with status {status}")
        transitioned = await self.queue.mark_failed(item.goal_id, terminal_error)
        if transitioned:
            self.state.failed += 1
            self.state.last_error = terminal_error
            self._record(
                "queue.failed",
                item,
                status="failed",
                payload={"goal_status": status},
            )
        return status, terminal_error, transitioned

    async def _goal_after_worker_cancel(
        self,
        item: RuntimeQueueItem,
        goal: dict[str, Any] | None,
    ) -> dict[str, Any]:
        queue_item = await self.queue.get_item(item.goal_id)
        if queue_item and queue_item.status not in {"pending", "running"}:
            return {
                **(goal or {}),
                "goal_id": item.goal_id,
                "user_id": (goal or {}).get("user_id") or item.user_id,
                "chat_id": (goal or {}).get("chat_id") or item.chat_id,
                "status": queue_item.status,
                "error": queue_item.error,
                "artifacts": (goal or {}).get("artifacts") or [],
            }
        return self._persist_interrupted_goal(item)

    async def _notify_terminal_goal(
        self,
        item: RuntimeQueueItem,
        goal: dict[str, Any],
        status: str,
    ) -> None:
        if not self.terminal_callback or status not in {"done", "blocked", "failed"}:
            return
        try:
            await asyncio.wait_for(
                self.terminal_callback(goal),
                timeout=self.notification_timeout,
            )
        except Exception as notify_error:
            self._record(
                "notification.failed",
                item,
                status=status,
                payload={"error_type": type(notify_error).__name__},
            )
            log.error(
                "runtime_terminal_notify_failed",
                goal_id=item.goal_id,
                status=status,
                error=type(notify_error).__name__,
            )
        else:
            self._record("notification.sent", item, status=status)

    def _start_terminal_notification(
        self,
        item: RuntimeQueueItem,
        goal: dict[str, Any],
        status: str,
    ) -> asyncio.Task[None] | None:
        if not self.terminal_callback or status not in {"done", "blocked", "failed"}:
            return None
        task = asyncio.create_task(
            self._notify_terminal_goal(item, goal, status),
            name=f"runtime-terminal-notify-{item.goal_id}",
        )
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)
        return task

    async def _stop_notifications(self) -> None:
        tasks = set(self._notification_tasks)
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.notification_stop_timeout,
            )
        except TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
                        if goal.get("status") in {"done", "failed", "cancelled"}:
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

    def _record(
        self,
        event_type: str,
        item: RuntimeQueueItem,
        *,
        status: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.event_recorder.record(
                event_type,
                goal_id=item.goal_id,
                skill=str(item.meta.get("skill") or ""),
                intent=str(item.meta.get("intent") or ""),
                status=status,
                user_id=item.user_id,
                chat_id=item.chat_id,
                payload=payload or {},
            )
        except Exception as error:
            log.warning(
                "runtime_event_record_failed",
                event_type=event_type,
                error=type(error).__name__,
            )

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
        event_recorder=None,
        notification_timeout: float = 30.0,
        notification_stop_timeout: float | None = None,
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
                event_recorder=event_recorder,
                notification_timeout=notification_timeout,
                notification_stop_timeout=notification_stop_timeout,
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
