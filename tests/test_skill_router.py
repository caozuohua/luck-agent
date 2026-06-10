from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch

from skills.base import GoalRequest, SkillContext, SkillMatch, SkillMetadata
from skills.legacy_react import LegacyReactSkill
from skills.registry import SkillRegistry
from skills.router import SkillRouteError, SkillRouter


class FakeSkill:
    def __init__(
        self,
        *,
        name: str,
        score: float = 0.0,
        priority: int = 100,
        matched: bool | None = None,
        raises: bool = False,
        execution_mode: str = "goal_runtime",
    ) -> None:
        self.metadata = SkillMetadata(
            name=name,
            version="1.0.0",
            intent=f"{name}_intent",
            description=f"{name} test skill",
            execution_mode=execution_mode,
            priority=priority,
        )
        self.score = score
        self.matched = score > 0 if matched is None else matched
        self.raises = raises
        self.calls = 0
        self.context: SkillContext | None = None

    def match(self, context: SkillContext) -> SkillMatch:
        self.calls += 1
        self.context = context
        if self.raises:
            raise RuntimeError("match failed")
        return SkillMatch(self.matched, self.score, self.metadata.name)

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


class MatchResultSkill(FakeSkill):
    def __init__(self, *, name: str, result: object) -> None:
        super().__init__(name=name)
        self.result = result

    def match(self, context: SkillContext) -> object:
        self.calls += 1
        self.context = context
        return self.result


class ContextCapturingSkill(FakeSkill):
    def __init__(self) -> None:
        super().__init__(name="capturing", score=0.8)
        self.context: SkillContext | None = None

    def match(self, context: SkillContext) -> SkillMatch:
        self.context = context
        return super().match(context)


class SkillRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = SkillContext("u", "c", "request")

    def test_router_uses_score_then_priority_then_name(self) -> None:
        registry = SkillRegistry(
            [
                FakeSkill(name="lower_score", score=0.7, priority=1),
                FakeSkill(name="zeta", score=0.8, priority=10),
                FakeSkill(name="alpha", score=0.8, priority=10),
                FakeSkill(name="higher_priority", score=0.8, priority=20),
                LegacyReactSkill(),
            ]
        )

        result = SkillRouter(registry).route(self.context)

        self.assertEqual(result.skill.metadata.name, "alpha")
        self.assertEqual(result.score, 0.8)
        self.assertEqual(result.reason, "alpha")
        self.assertEqual(result.intent, "alpha_intent")
        self.assertEqual(result.execution_mode, "goal_runtime")
        with self.assertRaises(FrozenInstanceError):
            result.score = 0.1

    def test_router_returns_explicit_legacy_fallback(self) -> None:
        fallback = LegacyReactSkill()
        registry = SkillRegistry(
            [FakeSkill(name="none", score=0.0), fallback]
        )

        result = SkillRouter(registry).route(self.context)

        self.assertIs(result.skill, fallback)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.skill.metadata.name, "legacy_react")
        self.assertEqual(result.execution_mode, "legacy_inline")

    def test_router_does_not_call_legacy_match(self) -> None:
        fallback = LegacyReactSkill()
        fallback.match = lambda context: self.fail("legacy match was called")
        selected = FakeSkill(name="selected", score=0.9)
        registry = SkillRegistry([selected, fallback])

        result = SkillRouter(registry).route(self.context)

        self.assertIs(result.skill, selected)

    def test_router_only_matches_goal_runtime_skills(self) -> None:
        legacy_inline = FakeSkill(
            name="other_legacy",
            score=1.0,
            execution_mode="legacy_inline",
        )
        selected = FakeSkill(name="selected", score=0.5)
        registry = SkillRegistry(
            [legacy_inline, selected, LegacyReactSkill()]
        )

        result = SkillRouter(registry).route(self.context)

        self.assertEqual(legacy_inline.calls, 0)
        self.assertIs(result.skill, selected)

    def test_router_isolates_match_exceptions(self) -> None:
        broken = FakeSkill(name="broken", raises=True)
        healthy = FakeSkill(name="healthy", score=0.6)
        registry = SkillRegistry([broken, healthy, LegacyReactSkill()])
        context = SkillContext(
            user_id="user",
            chat_id="chat",
            text="  MiXeD Request  ",
            message_id="message",
        )
        errors: list[tuple[object, SkillContext, Exception]] = []

        test_log = Mock()
        with patch("skills.router.log", test_log):
            result = SkillRouter(
                registry,
                match_error_handler=lambda skill, routed_context, error: errors.append(
                    (skill, routed_context, error)
                ),
            ).route(context)

        self.assertEqual(broken.calls, 1)
        self.assertIs(result.skill, healthy)
        self.assertEqual(errors[0][0], broken)
        self.assertIs(errors[0][1], broken.context)
        self.assertEqual(errors[0][1].user_id, "user")
        self.assertEqual(errors[0][1].chat_id, "chat")
        self.assertEqual(errors[0][1].message_id, "message")
        self.assertEqual(errors[0][1].text, "mixed request")
        self.assertIsInstance(errors[0][2], RuntimeError)
        test_log.warning.assert_called_once()
        self.assertEqual(
            test_log.warning.call_args.kwargs["skill"],
            "broken",
        )
        self.assertIn(
            "match failed",
            test_log.warning.call_args.kwargs["error"],
        )

    def test_router_isolates_invalid_match_results(self) -> None:
        class MissingAttributes:
            pass

        healthy = FakeSkill(name="healthy", score=0.6)
        malformed = [
            MatchResultSkill(name="none", result=None),
            MatchResultSkill(name="wrong_type", result="matched"),
            MatchResultSkill(name="missing_attributes", result=MissingAttributes()),
        ]
        registry = SkillRegistry(
            [*malformed, healthy, LegacyReactSkill()]
        )

        result = SkillRouter(registry).route(self.context)

        self.assertTrue(all(skill.calls == 1 for skill in malformed))
        self.assertIs(result.skill, healthy)

    def test_router_isolates_invalid_match_results_before_fallback(self) -> None:
        fallback = LegacyReactSkill()
        malformed = MatchResultSkill(name="none", result=None)
        errors: list[tuple[object, SkillContext, Exception]] = []
        registry = SkillRegistry(
            [malformed, fallback]
        )

        result = SkillRouter(
            registry,
            match_error_handler=lambda skill, context, error: errors.append(
                (skill, context, error)
            ),
        ).route(self.context)

        self.assertIs(result.skill, fallback)
        self.assertEqual(errors[0][0], malformed)
        self.assertIs(errors[0][1], malformed.context)
        self.assertIsInstance(errors[0][2], TypeError)

    def test_router_isolates_match_error_handler_exceptions(self) -> None:
        broken = FakeSkill(name="broken", raises=True)
        fallback = LegacyReactSkill()
        registry = SkillRegistry([broken, fallback])

        def failing_handler(
            skill: object,
            context: SkillContext,
            error: Exception,
        ) -> None:
            self.assertIs(context, broken.context)
            raise RuntimeError("handler failed")

        result = SkillRouter(
            registry,
            match_error_handler=failing_handler,
        ).route(self.context)

        self.assertIs(result.skill, fallback)

    def test_router_normalizes_context_text_before_matching(self) -> None:
        skill = ContextCapturingSkill()
        context = SkillContext(
            user_id="user",
            chat_id="chat",
            text="  MiXeD Request  ",
            message_id="message",
            model_override="model",
        )
        registry = SkillRegistry([skill, LegacyReactSkill()])

        SkillRouter(registry).route(context)

        self.assertEqual(skill.context.text, "mixed request")
        self.assertEqual(skill.context.user_id, context.user_id)
        self.assertEqual(skill.context.chat_id, context.chat_id)
        self.assertEqual(skill.context.message_id, context.message_id)
        self.assertEqual(skill.context.model_override, context.model_override)

    def test_router_rejects_invalid_scores(self) -> None:
        valid = FakeSkill(name="valid", score=0.4)
        registry = SkillRegistry(
            [
                FakeSkill(name="negative", score=-0.1, matched=True),
                FakeSkill(name="too_high", score=1.1, matched=True),
                FakeSkill(name="nan", score=float("nan"), matched=True),
                FakeSkill(name="infinite", score=float("inf"), matched=True),
                valid,
                LegacyReactSkill(),
            ]
        )

        result = SkillRouter(registry).route(self.context)

        self.assertIs(result.skill, valid)

    def test_router_ignores_unmatched_skill_with_positive_score(self) -> None:
        unmatched = FakeSkill(name="unmatched", score=1.0, matched=False)
        fallback = LegacyReactSkill()
        registry = SkillRegistry([unmatched, fallback])

        result = SkillRouter(registry).route(self.context)

        self.assertIs(result.skill, fallback)

    def test_router_requires_explicit_fallback_when_nothing_matches(self) -> None:
        registry = SkillRegistry([FakeSkill(name="none", score=0.0)])

        with self.assertRaisesRegex(SkillRouteError, "legacy_react"):
            SkillRouter(registry).route(self.context)


if __name__ == "__main__":
    unittest.main()
