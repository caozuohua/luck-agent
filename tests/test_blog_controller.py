from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from controllers.content_generator import GeneratedContent, ModelContentGenerator


class FakeRouter:
    def __init__(self, *, text: str = "1. AI Agent 长任务恢复机制") -> None:
        self.calls: list[dict] = []
        self.text = text

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
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
        self.assertIn("中文", router.calls[0]["system"])

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
