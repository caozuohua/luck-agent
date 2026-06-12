from __future__ import annotations

import re
from typing import Any, Protocol

from core.execution_engine import StepResult, StepSpec
from core.protocols import normalize_goal_title
from skills.base import (
    GoalRequest,
    SkillContext,
    SkillMatch,
    SkillMetadata,
)


BLOG_SUCCESS_CRITERIA = (
    "内容已生成或更新",
    "目标文件已写入",
    "本地构建或基础检查通过",
    "变更已提交并推送",
    "发布结果已验证或明确给出阻塞原因",
)


class GeneratedContent(Protocol):
    text: str
    model: str
    tokens: int


class ContentGenerator(Protocol):
    async def generate(self, goal: dict[str, Any]) -> GeneratedContent:
        ...


class BlogSkill:
    metadata = SkillMetadata(
        name="blog_write",
        version="1.0.0",
        intent="blog_write",
        description="Plan or generate blog content",
        execution_mode="goal_runtime",
        priority=50,
        timeout=180,
        max_retry=1,
    )

    _chinese_keywords = (
        "博客",
        "文章",
        "写文章",
        "博客选题",
        "重构博客",
        "发布博客",
    )
    _english_blog = re.compile(r"\bblog\b")
    _negated_chinese_blog = re.compile(
        r"(?:不要|不用|不需要|无需|别|禁止)"
        r"[^，。！？；：,.!?;:\n]{0,12}"
        r"(?:博客|文章|\bblog\b)"
    )
    _negated_english_blog = re.compile(
        r"\b(?:don't|do\s+not|not|no\s+need(?:\s+to)?|without)\b"
        r"(?:\s+[\w'-]+){0,4}\s+blog\b"
    )

    def __init__(self, *, generator: ContentGenerator) -> None:
        self.generator = generator

    def match(self, context: SkillContext) -> SkillMatch:
        text = (context.text or "").strip().lower()
        if (
            self._negated_chinese_blog.search(text)
            or self._negated_english_blog.search(text)
        ):
            return SkillMatch(False, reason="blog request explicitly negated")
        for keyword in self._chinese_keywords:
            if keyword in text:
                return SkillMatch(
                    matched=True,
                    score=0.95,
                    reason=f"blog keyword matched: {keyword}",
                )
        if self._english_blog.search(text):
            return SkillMatch(
                matched=True,
                score=0.95,
                reason="blog keyword matched: blog",
            )
        return SkillMatch(False, reason="no blog keyword matched")

    def build_goal(self, context: SkillContext) -> GoalRequest:
        source_message = context.text
        return GoalRequest(
            title=normalize_goal_title(source_message),
            intent=self.metadata.intent,
            success_criteria=BLOG_SUCCESS_CRITERIA,
            plan={"source_message": source_message},
        )

    async def build_plan(
        self,
        goal: dict[str, Any],
    ) -> list[StepSpec]:
        return [
            StepSpec(
                name="generate_content",
                action="generate_content",
                timeout=180,
                max_retry=1,
                replay_safe=True,
            )
        ]

    async def execute_step(
        self,
        goal: dict[str, Any],
        step: StepSpec,
    ) -> StepResult:
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

    async def is_goal_complete(
        self,
        goal: dict[str, Any],
        steps: list[dict[str, Any]],
    ) -> bool:
        if not steps:
            return False
        for step in steps:
            raw_input = step.get("input") or {}
            required = bool(raw_input.get("required", True))
            if required and step.get("status") != "done":
                return False
        return True
