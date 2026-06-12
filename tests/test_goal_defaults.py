from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.goal import GoalManager
from core.memory import Memory


class GoalDefaultCriteriaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.memory = Memory(str(Path(self.temp_dir.name) / "runtime.db"))
        self.goal_manager = GoalManager(self.memory)

    def tearDown(self) -> None:
        conn = getattr(self.memory._local, "conn", None)
        if conn is not None:
            conn.close()
            self.memory._local.conn = None
        self.temp_dir.cleanup()

    def test_default_success_criteria_are_domain_neutral(self) -> None:
        expected = GoalManager.default_success_criteria("unknown")

        for intent in ("blog_write", "github_code", "shell_run", "general"):
            with self.subTest(intent=intent):
                self.assertEqual(
                    GoalManager.default_success_criteria(intent),
                    expected,
                )

    def test_explicit_skill_success_criteria_are_persisted_unchanged(
        self,
    ) -> None:
        for criteria in (
            ["first skill criterion", "second skill criterion"],
            [],
        ):
            with self.subTest(criteria=criteria):
                goal_id = self.goal_manager.create_goal(
                    user_id="user-1",
                    chat_id="chat-1",
                    title="skill-owned goal",
                    intent="custom_skill",
                    success_criteria=criteria,
                )

                goal = self.goal_manager.get_goal(goal_id)
                self.assertEqual(goal["success_criteria"], criteria)

    def test_default_success_criteria_returns_a_fresh_list(self) -> None:
        first = GoalManager.default_success_criteria("one")
        second = GoalManager.default_success_criteria("two")

        first.append("mutated")

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
