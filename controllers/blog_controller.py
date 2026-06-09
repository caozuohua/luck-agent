"""Blog content generation controller."""
from __future__ import annotations

from controllers.base import BaseController
from core.execution_engine import StepResult, StepSpec


class BlogController(BaseController):
    intent = "blog_write"

    def __init__(self, *, generator) -> None:
        self.generator = generator

    async def build_plan(self, goal: dict) -> list[StepSpec]:
        return [
            StepSpec(
                name="generate_content",
                action="generate_content",
                timeout=180,
                max_retry=1,
            )
        ]

    async def execute_step(self, goal: dict, step: StepSpec) -> StepResult:
        if step.action != "generate_content":
            return StepResult(
                ok=False,
                action=step.action,
                error=f"unsupported action: {step.action}",
                blocking=True,
            )

        generated = await self.generator.generate(goal)
        artifact = {
            "type": "generated_content",
            "content": generated.text,
            "model": generated.model,
            "tokens": generated.tokens,
        }
        return StepResult(
            ok=True,
            action=step.action,
            data={"content": generated.text},
            artifacts=[artifact],
        )

    async def is_goal_complete(self, goal: dict, steps: list[dict]) -> bool:
        return self.all_required_steps_done(steps)
