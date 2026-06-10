from __future__ import annotations

import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError

from skills.base import GoalRequest, SkillContext, SkillMatch, SkillMetadata
from skills.legacy_react import LegacyReactSkill
from skills.registry import (
    SkillNotFoundError,
    SkillRegistrationError,
    SkillRegistry,
)


class FakeSkill:
    def __init__(
        self,
        *,
        name: str = "alpha",
        intent: str = "general",
        execution_mode: str = "goal_runtime",
    ) -> None:
        self.metadata = SkillMetadata(
            name=name,
            version="1.0.0",
            intent=intent,
            description=f"{name} test skill",
            execution_mode=execution_mode,
        )

    def match(self, context: SkillContext) -> SkillMatch:
        return SkillMatch(False)

    def build_goal(self, context: SkillContext) -> GoalRequest:
        return GoalRequest("title", self.metadata.intent)

    async def build_plan(self, goal: dict[str, object]) -> list[object]:
        return []

    async def execute_step(
        self,
        goal: dict[str, object],
        step: object,
    ) -> object:
        return object()

    async def is_goal_complete(
        self,
        goal: dict[str, object],
        steps: list[dict[str, object]],
    ) -> bool:
        return False


class SkillContractTests(unittest.TestCase):
    def test_importing_base_does_not_import_execution_engine_at_runtime(self) -> None:
        script = """
import builtins

original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "core.execution_engine":
        raise AssertionError("runtime execution_engine import")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import skills.base
"""

        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=".",
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_contracts_are_frozen_and_goal_plan_is_not_shared(self) -> None:
        metadata = SkillMetadata(
            name="alpha",
            version="1.0.0",
            intent="general",
            description="test skill",
            execution_mode="goal_runtime",
        )
        context = SkillContext("u1", "c1", "request")
        match = SkillMatch(True, 0.5, "matched")
        first_request = GoalRequest("title", "general")
        second_request = GoalRequest("title", "general")

        for instance in (metadata, context, match, first_request):
            with self.assertRaises(FrozenInstanceError):
                instance.unexpected = "value"

        first_request.plan["skill"] = "alpha"
        self.assertEqual(second_request.plan, {})


class SkillRegistryTests(unittest.TestCase):
    def test_constructor_registers_iterable_and_lists_in_registration_order(self) -> None:
        alpha = FakeSkill(name="alpha")
        beta = FakeSkill(name="beta")

        registry = SkillRegistry([alpha, beta])

        self.assertIs(registry.get("alpha"), alpha)
        self.assertEqual(registry.list(), [alpha, beta])

    def test_registry_rejects_duplicate_names(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeSkill(name="alpha"))

        with self.assertRaisesRegex(SkillRegistrationError, "duplicate skill"):
            registry.register(FakeSkill(name="alpha"))

    def test_registry_rejects_missing_or_non_metadata_value(self) -> None:
        registry = SkillRegistry()

        for skill in (object(), type("BadSkill", (), {"metadata": {}})()):
            with self.subTest(skill=skill):
                with self.assertRaisesRegex(
                    SkillRegistrationError, "metadata"
                ):
                    registry.register(skill)

    def test_registry_rejects_empty_metadata_fields(self) -> None:
        registry = SkillRegistry()
        field_values = {
            "name": "",
            "version": " ",
            "intent": "",
            "description": "\t",
        }

        for field_name, bad_value in field_values.items():
            values = {
                "name": "alpha",
                "version": "1.0.0",
                "intent": "general",
                "description": "test skill",
                "execution_mode": "goal_runtime",
            }
            values[field_name] = bad_value
            skill = type(
                "BadSkill",
                (),
                {"metadata": SkillMetadata(**values)},
            )()

            with self.subTest(field=field_name):
                with self.assertRaisesRegex(
                    SkillRegistrationError, field_name
                ):
                    registry.register(skill)

    def test_registry_rejects_invalid_execution_mode(self) -> None:
        registry = SkillRegistry()
        skill = FakeSkill(execution_mode="background")

        with self.assertRaisesRegex(SkillRegistrationError, "execution_mode"):
            registry.register(skill)

    def test_registry_requires_callable_match_for_all_skills(self) -> None:
        for execution_mode in ("goal_runtime", "legacy_inline"):
            skill = FakeSkill(execution_mode=execution_mode)
            skill.match = None

            with self.subTest(execution_mode=execution_mode):
                with self.assertRaisesRegex(
                    SkillRegistrationError,
                    "match",
                ):
                    SkillRegistry([skill])

    def test_registry_requires_goal_runtime_interface_methods(self) -> None:
        required_methods = (
            "build_goal",
            "build_plan",
            "execute_step",
            "is_goal_complete",
        )

        for method_name in required_methods:
            skill = FakeSkill()
            setattr(skill, method_name, None)

            with self.subTest(method=method_name):
                with self.assertRaisesRegex(
                    SkillRegistrationError,
                    method_name,
                ):
                    SkillRegistry([skill])

    def test_registry_requires_async_goal_runtime_methods(self) -> None:
        async_methods = (
            "build_plan",
            "execute_step",
            "is_goal_complete",
        )

        for method_name in async_methods:
            skill = FakeSkill()
            setattr(skill, method_name, lambda *args: None)

            with self.subTest(method=method_name):
                with self.assertRaisesRegex(
                    SkillRegistrationError,
                    method_name,
                ):
                    SkillRegistry([skill])

    def test_registry_legacy_inline_only_requires_match(self) -> None:
        skill = type(
            "LegacyOnlySkill",
            (),
            {
                "metadata": SkillMetadata(
                    name="legacy_only",
                    version="1.0.0",
                    intent="general",
                    description="legacy test skill",
                    execution_mode="legacy_inline",
                ),
                "match": lambda self, context: SkillMatch(False),
            },
        )()

        registry = SkillRegistry([skill])

        self.assertIs(registry.get("legacy_only"), skill)

    def test_registry_resolves_persisted_skill_before_intent(self) -> None:
        persisted = FakeSkill(name="new", intent="shared")
        by_intent = FakeSkill(name="old", intent="old")
        registry = SkillRegistry([persisted, by_intent])
        goal = {"intent": "old", "plan": {"skill": "new"}}

        self.assertIs(registry.resolve_goal(goal), persisted)

    def test_registry_resolves_old_goal_by_unique_intent(self) -> None:
        expected = FakeSkill(name="blog", intent="blog")
        registry = SkillRegistry(
            [expected, FakeSkill(name="other", intent="other")]
        )

        self.assertIs(registry.resolve_goal({"intent": "blog"}), expected)

    def test_registry_rejects_corrupt_persisted_skill_identity(self) -> None:
        expected = FakeSkill(name="blog", intent="blog")
        registry = SkillRegistry([expected])

        for persisted_name in ("", " ", None, [], {}):
            goal = {
                "intent": "blog",
                "plan": {"skill": persisted_name},
            }

            with self.subTest(persisted_name=persisted_name):
                with self.assertRaises(SkillNotFoundError):
                    registry.resolve_goal(goal)

    def test_registry_does_not_resolve_legacy_skill(self) -> None:
        registry = SkillRegistry([LegacyReactSkill()])

        with self.assertRaises(SkillNotFoundError):
            registry.resolve_goal(
                {"intent": "general", "plan": {"skill": "legacy_react"}}
            )

        with self.assertRaises(SkillNotFoundError):
            registry.resolve_goal({"intent": "general"})

    def test_registry_requires_unique_goal_skill_for_intent(self) -> None:
        registry = SkillRegistry(
            [
                FakeSkill(name="first", intent="shared"),
                FakeSkill(name="second", intent="shared"),
            ]
        )

        with self.assertRaisesRegex(SkillNotFoundError, "shared"):
            registry.resolve_goal({"intent": "shared"})


if __name__ == "__main__":
    unittest.main()
