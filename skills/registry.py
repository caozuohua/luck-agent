from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from skills.base import Skill, SkillMetadata


class SkillRegistrationError(ValueError):
    pass


class SkillNotFoundError(LookupError):
    pass


class SkillRegistry:
    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        metadata = getattr(skill, "metadata", None)
        self._validate_metadata(metadata)
        self._validate_skill_interface(skill, metadata)
        if metadata.name in self._skills:
            raise SkillRegistrationError(
                f"duplicate skill name: {metadata.name}"
            )
        self._skills[metadata.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def resolve_goal(self, goal: dict[str, Any]) -> Skill:
        plan = goal.get("plan")
        if isinstance(plan, dict) and "skill" in plan:
            persisted_name = plan["skill"]
            if (
                not isinstance(persisted_name, str)
                or not persisted_name.strip()
            ):
                raise SkillNotFoundError(
                    f"invalid persisted goal skill: {persisted_name!r}"
                )
            persisted = self.get(persisted_name)
            if (
                persisted is not None
                and persisted.metadata.execution_mode == "goal_runtime"
            ):
                return persisted
            raise SkillNotFoundError(
                f"goal skill not found: {persisted_name}"
            )

        intent = goal.get("intent", "")
        matches = [
            skill
            for skill in self._skills.values()
            if skill.metadata.execution_mode == "goal_runtime"
            and skill.metadata.intent == intent
        ]
        if len(matches) == 1:
            return matches[0]
        raise SkillNotFoundError(
            f"expected one goal skill for intent {intent!r}, "
            f"found {len(matches)}"
        )

    @staticmethod
    def _validate_metadata(metadata: object) -> None:
        if not isinstance(metadata, SkillMetadata):
            raise SkillRegistrationError(
                "skill metadata must be a SkillMetadata instance"
            )

        for field_name in ("name", "version", "intent", "description"):
            value = getattr(metadata, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SkillRegistrationError(
                    f"metadata {field_name} must be a non-empty string"
                )

        if metadata.execution_mode not in {
            "goal_runtime",
            "legacy_inline",
        }:
            raise SkillRegistrationError(
                "metadata execution_mode must be goal_runtime or legacy_inline"
            )
        if (
            not isinstance(metadata.priority, int)
            or isinstance(metadata.priority, bool)
        ):
            raise SkillRegistrationError("metadata priority must be an integer")
        if (
            not isinstance(metadata.timeout, int)
            or isinstance(metadata.timeout, bool)
            or metadata.timeout <= 0
        ):
            raise SkillRegistrationError(
                "metadata timeout must be a positive integer"
            )
        if (
            not isinstance(metadata.max_retry, int)
            or isinstance(metadata.max_retry, bool)
            or metadata.max_retry < 0
        ):
            raise SkillRegistrationError(
                "metadata max_retry must be a non-negative integer"
            )
        for field_name in ("required_permissions", "tool_allowlist"):
            value = getattr(metadata, field_name)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item.strip()
                for item in value
            ):
                raise SkillRegistrationError(
                    f"metadata {field_name} must be a tuple of non-empty strings"
                )

    @staticmethod
    def _validate_skill_interface(
        skill: object,
        metadata: SkillMetadata,
    ) -> None:
        required_methods = ["match"]
        if metadata.execution_mode == "goal_runtime":
            required_methods.extend(
                (
                    "build_goal",
                    "build_plan",
                    "execute_step",
                    "is_goal_complete",
                )
            )

        for method_name in required_methods:
            if not callable(getattr(skill, method_name, None)):
                raise SkillRegistrationError(
                    f"skill {metadata.name!r} requires callable "
                    f"{method_name}"
                )

        if metadata.execution_mode == "goal_runtime":
            for method_name in (
                "build_plan",
                "execute_step",
                "is_goal_complete",
            ):
                if not inspect.iscoroutinefunction(
                    getattr(skill, method_name)
                ):
                    raise SkillRegistrationError(
                        f"skill {metadata.name!r} requires async "
                        f"{method_name}"
                    )
