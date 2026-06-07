"""
runtime/runtime_manager.py — Goal Runtime entrypoint.

This manager is the migration bridge between the existing chat workflow and the
new Goal Runtime. Selected intents are converted into persistent goals and then
submitted to RuntimeTaskQueue so the chat handler can return immediately.
"""
from __future__ import annotations

from runtime.intent_router import RuntimeIntentRouter
from runtime.task_queue import RuntimeTaskQueue
from core.log import get_logger

log = get_logger()


class RuntimeManager:
    """Entry point for Goal Runtime execution."""

    def __init__(
        self,
        *,
        goal_manager,
        execution_engine=None,
        queue: RuntimeTaskQueue | None = None,
        intent_router: RuntimeIntentRouter | None = None,
    ) -> None:
        self.goal_manager = goal_manager
        self.execution_engine = execution_engine  # kept for compatibility; Worker owns execution now
        self.queue = queue or RuntimeTaskQueue(max_active=1)
        self.intent_router = intent_router or RuntimeIntentRouter()

    async def handle_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        text: str,
    ) -> dict:
        route = self.intent_router.route(text)

        if not route.use_goal_runtime:
            return {
                "handled": False,
                "intent": route.intent,
                "reason": route.reason,
            }

        goal_id = self.goal_manager.create_goal_from_message(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
            intent=route.intent,
        )

        item = await self.queue.submit(
            goal_id=goal_id,
            user_id=user_id,
            chat_id=chat_id,
            priority=self._priority_for_intent(route.intent),
            meta={
                "intent": route.intent,
                "reason": route.reason,
            },
        )
        log.info(
            "runtime_goal_accepted",
            goal_id=goal_id,
            intent=route.intent,
            queue_status=item.status,
        )

        return {
            "handled": True,
            "goal_id": goal_id,
            "intent": route.intent,
            "status": "accepted",
            "queue_status": item.status,
            "summary": self.goal_manager.summary(goal_id),
        }

    async def queue_snapshot(self) -> dict:
        return await self.queue.snapshot()

    @staticmethod
    def _priority_for_intent(intent: str) -> int:
        if intent == "blog_write":
            return 50
        return 100
