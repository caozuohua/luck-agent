from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from core.log import get_logger
from skills.base import Skill, SkillContext, SkillMatch
from skills.registry import SkillRegistry

log = get_logger()

MatchErrorHandler = Callable[[Skill, SkillContext, Exception], None]


class SkillRouteError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillRoute:
    skill: Skill
    score: float
    reason: str

    @property
    def intent(self) -> str:
        return self.skill.metadata.intent

    @property
    def execution_mode(self) -> str:
        return self.skill.metadata.execution_mode

    def to_dict(self) -> dict[str, object]:
        return {
            "skill": self.skill.metadata.name,
            "intent": self.intent,
            "execution_mode": self.execution_mode,
            "score": self.score,
            "reason": self.reason,
        }


class SkillRouter:
    def __init__(
        self,
        registry: SkillRegistry,
        match_error_handler: MatchErrorHandler | None = None,
    ) -> None:
        self.registry = registry
        self.match_error_handler = match_error_handler

    def route(self, context: SkillContext) -> SkillRoute:
        candidates: list[tuple[float, int, str, Skill, str]] = []
        fallback: Skill | None = None
        normalized_context = SkillContext(
            user_id=context.user_id,
            chat_id=context.chat_id,
            text=(context.text or "").strip().lower(),
            message_id=context.message_id,
            model_override=context.model_override,
        )

        for skill in self.registry.list():
            metadata = skill.metadata
            if metadata.name == "legacy_react":
                if metadata.execution_mode == "legacy_inline":
                    fallback = skill
                continue
            if metadata.execution_mode != "goal_runtime":
                continue

            try:
                match = skill.match(normalized_context)
            except Exception as error:
                self._handle_match_error(
                    skill,
                    normalized_context,
                    error,
                )
                continue
            if not isinstance(match, SkillMatch):
                self._handle_match_error(
                    skill,
                    normalized_context,
                    TypeError(
                        "match must return SkillMatch, "
                        f"got {type(match).__name__}"
                    ),
                )
                continue

            score = match.score
            if not isinstance(match.matched, bool):
                self._handle_match_error(
                    skill,
                    normalized_context,
                    TypeError("match.matched must be a bool"),
                )
                continue
            if (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
                or not 0.0 <= score <= 1.0
            ):
                self._handle_match_error(
                    skill,
                    normalized_context,
                    ValueError("match.score must be finite and between 0 and 1"),
                )
                continue
            if not isinstance(match.reason, str):
                self._handle_match_error(
                    skill,
                    normalized_context,
                    TypeError("match.reason must be a string"),
                )
                continue

            if match.matched:
                candidates.append(
                    (
                        -float(score),
                        metadata.priority,
                        metadata.name,
                        skill,
                        match.reason,
                    )
                )

        if candidates:
            negative_score, _, _, skill, reason = min(candidates)
            return SkillRoute(skill, -negative_score, reason)

        if fallback is None:
            raise SkillRouteError(
                "legacy_react fallback skill is not registered"
            )
        return SkillRoute(fallback, 0.0, "legacy fallback")

    def _handle_match_error(
        self,
        skill: Skill,
        context: SkillContext,
        error: Exception,
    ) -> None:
        log.warning(
            "skill_match_failed",
            skill=skill.metadata.name,
            error=f"{type(error).__name__}: {error}",
        )
        if self.match_error_handler is None:
            return
        try:
            self.match_error_handler(skill, context, error)
        except Exception as handler_error:
            log.warning(
                "skill_match_error_handler_failed",
                skill=skill.metadata.name,
                error=f"{type(handler_error).__name__}: {handler_error}",
            )
