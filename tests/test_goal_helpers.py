from __future__ import annotations

import unittest
from unittest.mock import patch

from core.goal import GoalManager


class GoalTitleHelperTests(unittest.TestCase):
    def test_title_from_message_delegates_to_protocol_helper(self) -> None:
        with patch(
            "core.goal.normalize_goal_title",
            return_value="shared title",
        ) as normalize:
            title = GoalManager._title_from_message("source", limit=23)

        self.assertEqual(title, "shared title")
        normalize.assert_called_once_with("source", limit=23)


if __name__ == "__main__":
    unittest.main()
