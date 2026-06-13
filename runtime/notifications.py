from __future__ import annotations

from typing import Any

from core.redaction import redact_text, redact_value
from core.short_id import short_id


class AcceptanceGatedNotifier:
    def __init__(self, *, wait_until_accepted: Any, notifier: Any) -> None:
        self.wait_until_accepted = wait_until_accepted
        self.notifier = notifier

    async def notify(self, goal: dict[str, Any]) -> None:
        goal_id = str(goal.get("goal_id") or "")
        await self.wait_until_accepted(goal_id)
        await self.notifier.notify(goal)


class RuntimeGoalNotifier:
    def __init__(self, *, sender: Any, card_builder: Any) -> None:
        self.sender = sender
        self.card_builder = card_builder

    async def notify(self, goal: dict[str, Any]) -> None:
        chat_id = str(goal.get("chat_id") or "").strip()
        if not chat_id:
            raise ValueError("goal chat_id is empty")

        goal_id = str(goal.get("goal_id") or "")
        status = str(goal.get("status") or "failed")

        if status == "done":
            artifacts = goal.get("artifacts") or []
            artifact = next(
                (
                    item
                    for item in reversed(artifacts)
                    if isinstance(item, dict)
                    and item.get("type") == "generated_content"
                    and str(item.get("content") or "").strip()
                ),
                None,
            )
            if artifact is not None:
                card = self.card_builder.agent_reply(
                    text=redact_text(artifact["content"]),
                    model=redact_text(artifact.get("model") or ""),
                    task_id=goal_id,
                )
            else:
                plan = goal.get("plan")
                skill = (
                    str(plan.get("skill") or "")
                    if isinstance(plan, dict)
                    else ""
                )
                task_type = redact_text(
                    skill or str(goal.get("intent") or "goal_runtime")
                )
                result = (
                    {"artifacts": redact_value(artifacts)}
                    if artifacts
                    else {"summary": "Goal completed"}
                )
                card = self.card_builder.task_status(
                    task_id=goal_id,
                    task_type=task_type,
                    status="done",
                    result=result,
                )
        else:
            detail_parts = [f"Goal ID: {short_id(goal_id)}"]
            current_step = redact_text(
                goal.get("current_step") or ""
            ).strip()
            if current_step:
                detail_parts.append(f"Current step: {current_step}")
            error = redact_text(
                goal.get("error") or f"goal ended with status {status}"
            )
            detail_parts.append(f"Error: {error}")
            card = self.card_builder.error(
                f"任务 {status}",
                "\n".join(detail_parts),
            )

        await self.sender.send(chat_id, card=card)
