"""
controllers/blog_controller.py — First GoalController implementation.

V1 scope intentionally stops before deployment:
1. inspect_repo
2. locate_article
3. rewrite_article
4. save_article

This validates GoalManager + ExecutionEngine end-to-end before introducing
GitHub push, CI verification, or publish checks.
"""
from __future__ import annotations

from core.execution_engine import StepResult, StepSpec
from controllers.base import BaseController


class BlogController(BaseController):
    intent = "blog_write"

    async def build_plan(self, goal: dict) -> list[StepSpec]:
        return [
            StepSpec(
                name="inspect_repo",
                action="inspect_repo",
                timeout=30,
            ),
            StepSpec(
                name="locate_article",
                action="locate_article",
                timeout=30,
            ),
            StepSpec(
                name="rewrite_article",
                action="rewrite_article",
                timeout=120,
            ),
            StepSpec(
                name="save_article",
                action="save_article",
                timeout=60,
            ),
        ]

    async def execute_step(self, goal: dict, step: StepSpec) -> StepResult:
        action = step.action

        if action == "inspect_repo":
            return StepResult(
                ok=True,
                action=action,
                data={
                    "intent": goal.get("intent"),
                    "title": goal.get("title"),
                },
            )

        if action == "locate_article":
            return StepResult(
                ok=True,
                action=action,
                data={
                    "article_found": True,
                },
            )

        if action == "rewrite_article":
            return StepResult(
                ok=True,
                action=action,
                data={
                    "rewrite_required": True,
                },
            )

        if action == "save_article":
            return StepResult(
                ok=True,
                action=action,
                artifacts=[
                    {
                        "type": "draft",
                        "title": goal.get("title", "blog_draft"),
                    }
                ],
            )

        return StepResult(
            ok=False,
            action=action,
            error=f"unsupported action: {action}",
            blocking=True,
        )

    async def is_goal_complete(self, goal: dict, steps: list[dict]) -> bool:
        return self.all_required_steps_done(steps)
