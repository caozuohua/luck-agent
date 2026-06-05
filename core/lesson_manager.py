"""
core/lesson_manager.py — Lessons retrieval and preflight context.

Supervisor already writes lessons after failures. LessonManager closes the loop:
retrieve relevant lessons before execution, mark them as used, and expose a
compact context for controllers or prompts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from core.log import get_logger

log = get_logger()


@dataclass
class PreflightContext:
    domain: str
    task_type: str
    lessons: list[dict[str, Any]] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt_text(self) -> str:
        if not self.lessons:
            return ""
        lines = [f"历史经验（domain={self.domain}, task_type={self.task_type}）："]
        for idx, lesson in enumerate(self.lessons, start=1):
            lines.append(f"{idx}. 错误模式：{lesson.get('error_pattern', '')}")
            if lesson.get("solution"):
                lines.append(f"   解决方案：{lesson.get('solution')}")
            if lesson.get("prevention"):
                lines.append(f"   预防措施：{lesson.get('prevention')}")
        if self.checklist:
            lines.append("执行前检查：")
            for item in self.checklist:
                lines.append(f"- {item}")
        return "\n".join(lines)


class LessonManager:
    """Retrieve and format lessons for preflight checks."""

    def __init__(self, memory, max_lessons: int = 5) -> None:
        self.memory = memory
        self.max_lessons = max_lessons

    def find_relevant_lessons(
        self,
        *,
        intent: str,
        action: str | None = None,
        query: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        domain = self.domain_from_intent(intent)
        task_type = intent or "general"
        search_query = query or action
        lessons = self.memory.search_lessons(
            domain=domain,
            task_type=task_type,
            query=search_query,
            limit=limit or self.max_lessons,
        )
        for lesson in lessons:
            lesson_id = lesson.get("lesson_id")
            if lesson_id:
                try:
                    self.memory.mark_lesson_used(int(lesson_id))
                except Exception as e:
                    log.warning("lesson_mark_used_failed", lesson_id=lesson_id, error=str(e))
        return lessons

    def build_preflight_context(
        self,
        *,
        intent: str,
        action: str | None = None,
        query: str | None = None,
    ) -> PreflightContext:
        domain = self.domain_from_intent(intent)
        task_type = intent or "general"
        lessons = self.find_relevant_lessons(
            intent=intent,
            action=action,
            query=query,
        )
        checklist = self._checklist_from_lessons(lessons)
        return PreflightContext(
            domain=domain,
            task_type=task_type,
            lessons=lessons,
            checklist=checklist,
        )

    def record_manual_lesson(
        self,
        *,
        intent: str,
        error_pattern: str,
        solution: str,
        root_cause: str = "",
        prevention: str = "",
        confidence: float = 0.8,
    ) -> int:
        lesson = {
            "domain": self.domain_from_intent(intent),
            "task_type": intent or "general",
            "error_pattern": error_pattern,
            "root_cause": root_cause,
            "solution": solution,
            "prevention": prevention,
            "confidence": confidence,
        }
        return self.memory.save_lesson(lesson)

    @staticmethod
    def domain_from_intent(intent: str) -> str:
        if (intent or "").startswith("blog"):
            return "blog"
        if (intent or "").startswith("github") or intent in {"git_push", "github_code"}:
            return "github"
        if (intent or "").startswith("shell"):
            return "shell"
        if (intent or "").startswith("pkb"):
            return "pkb"
        return "general"

    @staticmethod
    def _checklist_from_lessons(lessons: list[dict[str, Any]]) -> list[str]:
        checklist: list[str] = []
        for lesson in lessons:
            prevention = (lesson.get("prevention") or "").strip()
            if prevention and prevention not in checklist:
                checklist.append(prevention)
        return checklist[:5]
