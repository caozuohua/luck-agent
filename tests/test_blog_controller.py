from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from controllers.blog_controller import BlogController
from controllers.content_generator import GeneratedContent, ModelContentGenerator
from core.execution_engine import StepSpec


class FakeRouter:
    def __init__(self, *, text: str = "1. AI Agent 长任务恢复机制") -> None:
        self.calls: list[dict] = []
        self.text = text

    async def chat(
        self,
        model_name: str,
        messages: list[dict],
        tools_schema: list[dict] | None = None,
        system: str = "",
        user_id: str = "",
    ) -> dict:
        self.calls.append(
            {
                "model_name": model_name,
                "messages": messages,
                "tools_schema": tools_schema,
                "system": system,
                "user_id": user_id,
            }
        )
        return {
            "text": self.text,
            "tool_calls": [],
            "model": "fake-model",
            "tokens": 12,
        }


class ModelContentGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_uses_original_goal_message(self) -> None:
        router = FakeRouter()
        generator = ModelContentGenerator(router=router, model_name="fake-model")
        goal = {
            "user_id": "u1",
            "title": "回退标题",
            "plan": {"source_message": "帮我整理一个博客选题"},
        }

        result = await generator.generate(goal)

        self.assertIn("AI Agent", result.text)
        self.assertEqual(result.model, "fake-model")
        self.assertEqual(result.tokens, 12)
        self.assertEqual(
            router.calls[0]["messages"],
            [{"role": "user", "content": "帮我整理一个博客选题"}],
        )
        self.assertEqual(router.calls[0]["tools_schema"], [])
        self.assertEqual(router.calls[0]["model_name"], "fake-model")
        self.assertEqual(router.calls[0]["user_id"], "u1")
        self.assertIn("遵循用户请求的语言", router.calls[0]["system"])
        self.assertNotIn("中文结果", router.calls[0]["system"])
        self.assertIn("5-10 个具体选题", router.calls[0]["system"])
        self.assertIn("标题、切入点和目标读者", router.calls[0]["system"])

    async def test_generate_falls_back_to_title(self) -> None:
        router = FakeRouter()
        generator = ModelContentGenerator(router=router, model_name="fake-model")

        await generator.generate(
            {"user_id": "u1", "title": "使用这个标题", "plan": {}}
        )

        self.assertEqual(
            router.calls[0]["messages"],
            [{"role": "user", "content": "使用这个标题"}],
        )

    async def test_generate_falls_back_to_title_for_blank_source_message(
        self,
    ) -> None:
        router = FakeRouter()
        generator = ModelContentGenerator(router=router, model_name="fake-model")

        await generator.generate(
            {
                "user_id": "u1",
                "title": "有效标题",
                "plan": {"source_message": " \t\n "},
            }
        )

        self.assertEqual(
            router.calls[0]["messages"],
            [{"role": "user", "content": "有效标题"}],
        )

    async def test_generate_rejects_empty_input(self) -> None:
        generator = ModelContentGenerator(
            router=FakeRouter(),
            model_name="fake-model",
        )

        with self.assertRaisesRegex(ValueError, "source message is empty"):
            await generator.generate({"title": "  ", "plan": {}})

    async def test_generate_rejects_empty_model_text(self) -> None:
        generator = ModelContentGenerator(
            router=FakeRouter(text="  "),
            model_name="fake-model",
        )

        with self.assertRaisesRegex(ValueError, "model returned empty content"):
            await generator.generate({"title": "生成内容"})

    def test_generated_content_is_frozen(self) -> None:
        content = GeneratedContent(text="正文", model="fake-model", tokens=12)

        with self.assertRaises(FrozenInstanceError):
            content.text = "修改"


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


class BlogControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_contains_one_real_generation_step(self) -> None:
        controller = BlogController(generator=FakeGenerator())

        plan = await controller.build_plan({"goal_id": "g1"})

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].name, "generate_content")
        self.assertEqual(plan[0].action, "generate_content")
        self.assertEqual(plan[0].timeout, 180)
        self.assertEqual(plan[0].max_retry, 1)

    async def test_generate_step_returns_persistable_artifact(self) -> None:
        generator = FakeGenerator("选题 A\n选题 B")
        controller = BlogController(generator=generator)
        goal = {"goal_id": "g1", "title": "整理博客选题"}
        step = StepSpec(name="generate_content", action="generate_content")

        result = await controller.execute_step(goal, step)

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
        controller = BlogController(generator=FakeGenerator())

        result = await controller.execute_step(
            {"goal_id": "g1"},
            StepSpec(name="inspect_repo", action="inspect_repo"),
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.blocking)
        self.assertEqual(result.error, "unsupported action: inspect_repo")

    async def test_goal_completion_uses_required_step_statuses(self) -> None:
        controller = BlogController(generator=FakeGenerator())

        self.assertTrue(
            await controller.is_goal_complete(
                {"goal_id": "g1"},
                [{"status": "done", "input": {"required": True}}],
            )
        )
        self.assertFalse(
            await controller.is_goal_complete(
                {"goal_id": "g1"},
                [{"status": "pending", "input": {"required": True}}],
            )
        )
