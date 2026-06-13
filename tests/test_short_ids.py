from __future__ import annotations

import unittest

from core.short_id import short_id


class ShortIdTests(unittest.TestCase):
    def test_short_id_removes_type_prefix_and_shows_four_characters(self) -> None:
        self.assertEqual(short_id("goal_a1b2c3d4e5"), "a1b2")
        self.assertEqual(short_id("abcd1234"), "abcd")
