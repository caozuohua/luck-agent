from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.model_router import ModelRouter


class ModelRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_falls_back_when_model_returns_empty_response(
        self,
    ) -> None:
        router = ModelRouter.__new__(ModelRouter)
        router._call = AsyncMock(
            side_effect=[
                {
                    "text": "",
                    "tool_calls": [],
                    "model": "gemini-3.5-flash",
                    "tokens": 0,
                },
                {
                    "text": "备用模型生成的博客选题",
                    "tool_calls": [],
                    "model": "gemini-2.5-pro",
                    "tokens": 42,
                },
            ]
        )

        with patch("core.model_router.asyncio.sleep", new=AsyncMock()):
            result = await router.chat(
                model_name="gemini-3.5-flash",
                messages=[{"role": "user", "content": "帮我整理一个博客选题"}],
                tools_schema=[],
                user_id="u1",
            )

        self.assertEqual(result["text"], "备用模型生成的博客选题")
        self.assertEqual(result["model"], "gemini-2.5-pro")
        self.assertEqual(router._call.await_count, 2)


if __name__ == "__main__":
    unittest.main()
