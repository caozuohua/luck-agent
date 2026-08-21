"""Goal Runtime adapter that executes Goals through LangGraph.

This is the production bridge between the current SQLite GoalStore and the
LangGraph executor. It intentionally does not use the legacy synchronous
``core.goal.GoalManager`` path.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from core.graph.contract import DECISION_DONE
from core.graph.executor import GraphExecutionRequest, GraphGoalExecutor
from core.log import get_logger
from memory.goal_store import Goal, GoalStatus, GoalStore
from runtime.contracts import RuntimeHandleResult
from runtime.events import NoopRuntimeEventRecorder
from runtime.task_queue import RuntimeQueueItem, RuntimeTaskQueue

log = get_logger("runtime.graph_runtime")

TerminalCallback = Callable[[dict[str, Any]], Awaitable[None]]


class GraphRuntime:
    """Queue, execute, recover, and notify current SQLite Goals."""

    def __init__(
        self,
        *,
        goal_store: GoalStore,
        graph_executor: GraphGoalExecutor,
        max_active: int = 1,
        terminal_callback: TerminalCallback | None = None,
        event_recorder=None,
    ) -> None:
        self.goal_store = goal_store
        self.graph_executor = graph_executor
        self.queue = RuntimeTaskQueue(max_active=max_active)
        self.terminal_callback = terminal_callback
        self.event_recorder = event_recorder or NoopRuntimeEventRecorder()
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._user_semaphores: dict[str, asyncio.Semaphore] = {}

    async def start(self) -> int:
        if any(not task.done() for task in self._worker_tasks):
            return 0
        self._stop_event.clear()
        self._worker_tasks = [
            asyncio.create_task(
                self._worker_loop(),
                name=f"graph-runtime-worker-{index}",
            )
            for index in range(self.queue.max_active)
        ]
        recovered = await self.recover_goals()
        log.info("graph_runtime_started", recovered=recovered)
        return recovered

    async def stop(self) -> None:
        self._stop_event.set()
        tasks = self._worker_tasks
        self._worker_tasks = []
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("graph_runtime_stopped")

    async def handle_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
        approval_token: str | None = None,
    ) -> RuntimeHandleResult:
        """Persist and enqueue one natural-language Goal."""
        goal = await self.goal_store.create(user_id, text, chat_id=chat_id)
        event_fields = {
            "goal_id": goal.id,
            "user_id": user_id,
            "chat_id": chat_id,
        }
        try:
            await self.goal_store.update_status(
                goal.id,
                GoalStatus.ROUTING,
                intent_type="general",
            )
            await self.goal_store.update_status(
                goal.id,
                GoalStatus.PLANNING,
                plan=json.dumps(
                    {
                        "executor": "langgraph",
                        "version": "1",
                        "source_message_id": message_id,
                    },
                    ensure_ascii=False,
                ),
            )
            item = await self.queue.submit(
                goal_id=goal.id,
                user_id=user_id,
                chat_id=chat_id,
                meta={
                    "executor": "langgraph",
                    "approval_token": approval_token or "",
                },
            )
        except Exception as error:
            await self._fail_goal(goal.id, f"queue submission failed: {error}")
            self._record("goal.failed", **event_fields, status="failed")
            raise

        self._record(
            "goal.accepted",
            **event_fields,
            status="accepted",
        )
        return RuntimeHandleResult(
            handled=True,
            skill="langgraph",
            goal_id=goal.id,
            intent="general",
            status="accepted",
            queue_status=item.status,
            summary=(
                "✅ 任务已接收，正在后台执行。\n"
                f"• Goal：`{goal.id[:8]}`\n"
                f"• 队列：`{item.status}`"
            ),
            reason="graph runtime",
        )

    async def recover_goals(self) -> int:
        """Requeue every non-terminal Goal after process restart."""
        goals = await self.goal_store.get_in_progress_all()
        recovered = 0
        for goal in goals:
            reset = await self.goal_store.reset_for_recovery(goal.id)
            if reset is None:
                continue
            try:
                await self.queue.submit(
                    goal_id=goal.id,
                    user_id=goal.user_id,
                    chat_id=goal.chat_id,
                    meta={"executor": "langgraph", "recovered": True},
                )
            except ValueError:
                # The in-memory queue may already contain a recovered item.
                continue
            self._record(
                "goal.recovered",
                goal_id=goal.id,
                user_id=goal.user_id,
                chat_id=goal.chat_id,
                status="pending",
            )
            recovered += 1
        return recovered

    async def queue_snapshot(self) -> dict[str, Any]:
        return await self.queue.snapshot()

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            item = await self.queue.get()
            await self._execute_item(item)

    async def _execute_item(self, item: RuntimeQueueItem) -> None:
        semaphore = self._user_semaphores.setdefault(
            item.user_id,
            asyncio.Semaphore(self.queue.max_active),
        )
        async with semaphore:
            try:
                goal = await self.goal_store.get(item.goal_id)
                if goal is None:
                    await self.queue.mark_failed(item.goal_id, "goal not found")
                    return
                if goal.status in {GoalStatus.DONE, GoalStatus.FAILED}:
                    await self.queue.mark_done(item.goal_id)
                    return
                await self.goal_store.update_status(goal.id, GoalStatus.EXECUTING)
                token = str(item.meta.get("approval_token") or "") or None
                state = await self.graph_executor.execute(
                    GraphExecutionRequest(
                        goal_id=goal.id,
                        user_id=goal.user_id,
                        text=goal.raw_input,
                        approval_token=token,
                    ),
                    hitl=False,
                )
                answer = str(
                    state.get("final_answer")
                    or "（任务未能完成，请换一种说法或提供更多上下文。）"
                )
                decision = str(state.get("decision") or "")
                await self.goal_store.update_status(
                    goal.id,
                    GoalStatus.AWAITING_RESULT,
                )
                await self.goal_store.update_status(
                    goal.id,
                    GoalStatus.EVALUATING,
                )
                if decision != DECISION_DONE:
                    await self.goal_store.update_status(
                        goal.id,
                        GoalStatus.FAILED,
                        result=answer,
                        error=answer,
                    )
                    await self.queue.mark_failed(item.goal_id, answer)
                    status = GoalStatus.FAILED.value
                else:
                    await self.goal_store.update_status(
                        goal.id,
                        GoalStatus.DONE,
                        result=answer,
                    )
                    await self.queue.mark_done(item.goal_id)
                    status = GoalStatus.DONE.value
                final_goal = await self.goal_store.get(goal.id)
                if final_goal is not None:
                    await self._notify(final_goal, status)
            except asyncio.CancelledError:
                await self.queue.mark_interrupted(
                    item.goal_id,
                    "graph runtime stopped",
                )
                raise
            except Exception as error:
                message = f"graph execution failed: {type(error).__name__}"
                await self._fail_goal(item.goal_id, message)
                await self.queue.mark_failed(item.goal_id, message)
                final_goal = await self.goal_store.get(item.goal_id)
                if final_goal is not None:
                    await self._notify(final_goal, GoalStatus.FAILED.value)
                log.error("graph_runtime_goal_failed", goal_id=item.goal_id, error=message)

    async def _fail_goal(self, goal_id: str, error: str) -> None:
        goal = await self.goal_store.get(goal_id)
        if goal is None or goal.status in {GoalStatus.DONE, GoalStatus.FAILED}:
            return
        if goal.status == GoalStatus.IDLE:
            await self.goal_store.update_status(goal_id, GoalStatus.ROUTING)
            goal = await self.goal_store.get(goal_id)
        if goal is not None and goal.status == GoalStatus.ROUTING:
            await self.goal_store.update_status(goal_id, GoalStatus.FAILED, error=error)
        elif goal is not None and goal.status in {
            GoalStatus.PLANNING,
            GoalStatus.EXECUTING,
            GoalStatus.AWAITING_RESULT,
            GoalStatus.EVALUATING,
        }:
            await self.goal_store.update_status(goal_id, GoalStatus.FAILED, error=error)

    async def _notify(self, goal: Goal, status: str) -> None:
        if self.terminal_callback is None or not goal.user_id:
            return
        try:
            await self.terminal_callback(_goal_dict(goal, status=status))
        except Exception as error:
            log.warning(
                "graph_runtime_notification_failed",
                goal_id=goal.id,
                error=type(error).__name__,
            )

    def _record(self, event_type: str, **fields: Any) -> None:
        try:
            self.event_recorder.record(event_type, **fields)
        except Exception:
            log.debug("graph_runtime_event_record_failed", event_type=event_type)


def _goal_dict(goal: Goal, *, status: str) -> dict[str, Any]:
    return {
        "goal_id": goal.id,
        "user_id": goal.user_id,
        "chat_id": goal.chat_id,
        "status": status,
        "intent": goal.intent_type,
        "raw_input": goal.raw_input,
        "result": goal.result,
        "error": goal.error,
        "updated_at": goal.updated_at,
    }
