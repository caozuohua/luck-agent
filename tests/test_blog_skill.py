from __future__ import annotations

import ast
import inspect
from pathlib import Path
import unittest
from typing import get_type_hints

from controllers.content_generator import GeneratedContent
from core.execution_engine import StepSpec
from core.goal import GoalManager
from core.protocols import normalize_goal_title
from skills import BlogSkill as PublicBlogSkill
from skills.base import GoalRequest, SkillContext
from skills.blog import BLOG_SUCCESS_CRITERIA, BlogSkill, ContentGenerator
from skills.legacy_react import LegacyReactSkill
from skills.registry import SkillRegistry
from skills.router import SkillRouter


class FakeGenerator:
    def __init__(self, text: str = "选题结果") -> None:
        self.text = text
        self.goals: list[dict] = []

    async def generate(self, goal: dict) -> GeneratedContent:
        self.goals.append(goal)
        return GeneratedContent(
            text=self.text,
            model="fake-model",
            tokens=8,
        )


class BlogSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = BlogSkill(generator=FakeGenerator())

    def test_metadata_matches_runtime_contract(self) -> None:
        metadata = self.skill.metadata

        self.assertEqual(metadata.name, "blog_write")
        self.assertEqual(metadata.version, "1.0.0")
        self.assertEqual(metadata.intent, "blog_write")
        self.assertEqual(metadata.description, "Plan or generate blog content")
        self.assertEqual(metadata.execution_mode, "goal_runtime")
        self.assertEqual(metadata.priority, 50)
        self.assertEqual(metadata.timeout, 180)
        self.assertEqual(metadata.max_retry, 1)
        self.assertTrue(inspect.iscoroutinefunction(self.skill.build_plan))
        self.assertTrue(inspect.iscoroutinefunction(self.skill.execute_step))
        self.assertTrue(
            inspect.iscoroutinefunction(self.skill.is_goal_complete)
        )
        self.assertIs(SkillRegistry([self.skill]).get("blog_write"), self.skill)

    def test_blog_skill_is_exported_from_skills_package(self) -> None:
        self.assertIs(PublicBlogSkill, BlogSkill)

    def test_all_blog_keywords_match_with_fixed_score(self) -> None:
        for text in (
            "博客",
            "blog",
            "文章",
            "写文章",
            "博客选题",
            "重构博客",
            "发布博客",
        ):
            with self.subTest(text=text):
                match = self.skill.match(SkillContext("u", "c", text))
                self.assertTrue(match.matched)
                self.assertEqual(match.score, 0.95)
                self.assertIn("keyword", match.reason.lower())

    def test_match_normalizes_english_case_and_whitespace(self) -> None:
        match = self.skill.match(
            SkillContext("u", "c", " \t Please Draft A BLOG Post \n")
        )

        self.assertTrue(match.matched)
        self.assertEqual(match.score, 0.95)

    def test_english_blog_requires_a_complete_word(self) -> None:
        for text in ("blogger", "weblog"):
            with self.subTest(text=text):
                self.assertFalse(
                    self.skill.match(SkillContext("u", "c", text)).matched
                )

    def test_explicitly_negated_blog_requests_do_not_match(self) -> None:
        for text in (
            "不要写博客，查看服务器状态",
            "不需要 blog，检查服务",
            "不要修改这篇文章，只检查链接",
            "无需发布博客，只做构建",
            "do not edit the blog, check links",
            "don't publish a blog, run tests",
            "not writing the blog, check links",
            "no need to update the blog, build only",
            "without changing the blog, inspect links",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    self.skill.match(SkillContext("u", "c", text)).matched
                )

    def test_ordinary_blog_topic_still_matches(self) -> None:
        self.assertTrue(
            self.skill.match(SkillContext("u", "c", "blog topic")).matched
        )

    def test_non_blog_message_uses_legacy_fallback(self) -> None:
        registry = SkillRegistry([self.skill, LegacyReactSkill()])

        route = SkillRouter(registry).route(
            SkillContext("u", "c", "帮我查看服务器状态")
        )

        self.assertEqual(route.skill.metadata.name, "legacy_react")
        self.assertEqual(route.score, 0.0)

    def test_build_goal_returns_independent_goal_requests(self) -> None:
        first = self.skill.build_goal(
            SkillContext("u", "c", "帮我整理一个博客选题")
        )
        second = self.skill.build_goal(
            SkillContext("u", "c", "写另一篇文章")
        )

        self.assertIsInstance(first, GoalRequest)
        self.assertIsNot(first.plan, second.plan)
        first.plan["changed"] = True
        self.assertNotIn("changed", second.plan)

    def test_build_goal_normalizes_title_and_preserves_source_message(
        self,
    ) -> None:
        source = "  帮我\n整理\t一个博客选题  "

        request = self.skill.build_goal(SkillContext("u", "c", source))

        self.assertEqual(request.title, "帮我 整理 一个博客选题")
        self.assertEqual(request.intent, "blog_write")
        self.assertEqual(
            request.success_criteria,
            (
                "内容已生成或更新",
                "目标文件已写入",
                "本地构建或基础检查通过",
                "变更已提交并推送",
                "发布结果已验证或明确给出阻塞原因",
            ),
        )
        self.assertEqual(request.success_criteria, BLOG_SUCCESS_CRITERIA)
        self.assertIsInstance(request.success_criteria, tuple)
        self.assertEqual(request.plan, {"source_message": source})

    def test_build_goal_uses_unnamed_title_for_blank_message(self) -> None:
        request = self.skill.build_goal(
            SkillContext("u", "c", " \t\n ")
        )

        self.assertEqual(request.title, "未命名目标")
        self.assertEqual(request.plan["source_message"], " \t\n ")

    def test_build_goal_truncates_long_title_to_sixty_characters(self) -> None:
        source = "博" * 61

        request = self.skill.build_goal(SkillContext("u", "c", source))

        self.assertEqual(request.title, ("博" * 60) + "…")

    def test_blog_module_has_no_core_goal_import(self) -> None:
        source_path = Path(inspect.getfile(BlogSkill))
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )

        self.assertNotIn("core.goal", imported_modules)

    def test_generator_constructor_uses_content_generator_protocol(self) -> None:
        hints = get_type_hints(BlogSkill.__init__)

        self.assertIs(hints["generator"], ContentGenerator)


class GoalTitleTests(unittest.TestCase):
    def test_normalize_goal_title_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_goal_title("  帮我\n整理\t一个博客选题  "),
            "帮我 整理 一个博客选题",
        )

    def test_normalize_goal_title_handles_blank_and_limit(self) -> None:
        self.assertEqual(normalize_goal_title(" \t\n "), "未命名目标")
        self.assertEqual(
            normalize_goal_title("博" * 61),
            ("博" * 60) + "…",
        )

    def test_goal_manager_and_blog_skill_share_title_normalization(self) -> None:
        skill = BlogSkill(generator=FakeGenerator())

        for source in (" \t\n ", "博" * 61):
            with self.subTest(source=source):
                self.assertEqual(
                    GoalManager._title_from_message(source),
                    skill.build_goal(SkillContext("u", "c", source)).title,
                )


class BlogSkillExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_contains_one_generation_step(self) -> None:
        skill = BlogSkill(generator=FakeGenerator())

        plan = await skill.build_plan({"goal_id": "g1"})

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].name, "generate_content")
        self.assertEqual(plan[0].action, "generate_content")
        self.assertEqual(plan[0].timeout, 180)
        self.assertEqual(plan[0].max_retry, 1)

    async def test_generate_step_returns_persistable_artifact(self) -> None:
        generator = FakeGenerator("选题 A\n选题 B")
        skill = BlogSkill(generator=generator)
        goal = {"goal_id": "g1", "title": "整理博客选题"}

        result = await skill.execute_step(
            goal,
            StepSpec(name="generate_content", action="generate_content"),
        )

        self.assertEqual(generator.goals, [goal])
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "generate_content")
        self.assertEqual(result.data, {"content": "选题 A\n选题 B"})
        self.assertEqual(
            result.artifacts,
            [
                {
                    "type": "generated_content",
                    "content": "选题 A\n选题 B",
                    "model": "fake-model",
                    "tokens": 8,
                }
            ],
        )

    async def test_unsupported_step_is_blocking(self) -> None:
        skill = BlogSkill(generator=FakeGenerator())

        result = await skill.execute_step(
            {"goal_id": "g1"},
            StepSpec(name="inspect_repo", action="inspect_repo"),
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocking)
        self.assertEqual(result.error, "unsupported action: inspect_repo")

    async def test_goal_completion_requires_all_required_steps_done(
        self,
    ) -> None:
        skill = BlogSkill(generator=FakeGenerator())

        self.assertFalse(await skill.is_goal_complete({}, []))
        self.assertTrue(
            await skill.is_goal_complete(
                {},
                [
                    {"status": "done", "input": {"required": True}},
                    {"status": "pending", "input": {"required": False}},
                ],
            )
        )
        self.assertFalse(
            await skill.is_goal_complete(
                {},
                [{"status": "pending", "input": {"required": True}}],
            )
        )


if __name__ == "__main__":
    unittest.main()
