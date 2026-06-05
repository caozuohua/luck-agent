"""
runtime/runtime_manager.py — Goal Runtime entrypoint.

This manager is the migration bridge between the existing chat workflow and the
new Goal Runtime. Only selected intents (initially blog_write) are routed into
GoalManager + ExecutionEngine.
"""
from __future__ import annotations

from runtime.intent_router import RuntimeIntentRouter


class RuntimeManager:
    """Entry point for Goal Runtime execution."""

    def __init__(
        self,
        *,
        goal_manager,
        execution_engine,
        intent_router: RuntimeIntentRouter | None = None,
    ) -> None:
        self.goal_manager = goal_manager
        self.execution_engine = execution_engine
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

        await self.execution_engine.run_goal(goal_id)

        goal = self.goal_manager.get_goal(goal_id)

        return {
            "handled": True,
            "goal_id": goal_id,
            "intent": route.intent,
            "status": goal.get("status"),
            "summary": self.goal_manager.summary(goal_id),
        }
