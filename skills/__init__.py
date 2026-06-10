from typing import TYPE_CHECKING, Any

from skills.base import (
    GoalRequest,
    GoalSkill,
    Skill,
    SkillContext,
    SkillMatch,
    SkillMetadata,
)
from skills.legacy_react import LegacyReactSkill
from skills.registry import (
    SkillNotFoundError,
    SkillRegistrationError,
    SkillRegistry,
)
from skills.router import SkillRoute, SkillRouteError, SkillRouter

if TYPE_CHECKING:
    from skills.blog import BlogSkill


def __getattr__(name: str) -> Any:
    if name == "BlogSkill":
        from skills.blog import BlogSkill

        return BlogSkill
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BlogSkill",
    "GoalRequest",
    "GoalSkill",
    "LegacyReactSkill",
    "Skill",
    "SkillContext",
    "SkillMatch",
    "SkillMetadata",
    "SkillNotFoundError",
    "SkillRegistrationError",
    "SkillRegistry",
    "SkillRoute",
    "SkillRouteError",
    "SkillRouter",
]
