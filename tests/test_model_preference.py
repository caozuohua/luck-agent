from __future__ import annotations

import unittest

from agent import resolve_model_preference


class FakeMemory:
    def __init__(self) -> None:
        self.profile: dict[tuple[str, str], str] = {}

    def set_profile(self, user_id: str, key: str, value: str) -> None:
        self.profile[(user_id, key)] = value

    def get_profile(self, user_id: str, key: str, default: str = "") -> str:
        return self.profile.get((user_id, key), default)


class ModelPreferenceTests(unittest.TestCase):
    def test_model_prefix_persists_for_following_messages(self) -> None:
        memory = FakeMemory()
        models = {"/pro": "pro-model", "/flash": "flash-model", "/lite": "lite-model"}

        text, model, changed = resolve_model_preference(
            memory, "user", "/pro 深度分析", models
        )
        next_text, next_model, next_changed = resolve_model_preference(
            memory, "user", "继续", models
        )

        self.assertEqual((text, model, changed), ("深度分析", "pro-model", True))
        self.assertEqual((next_text, next_model, next_changed), ("继续", "pro-model", False))
