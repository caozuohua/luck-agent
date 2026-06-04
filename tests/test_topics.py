from __future__ import annotations

import unittest

from core.topics import normalize_topics


class NormalizeTopicsTests(unittest.TestCase):
    def test_string_topics_are_split_and_normalized(self) -> None:
        self.assertEqual(normalize_topics("python, AI\nllm"), ["Python", "AI", "LLM"])


if __name__ == "__main__":
    unittest.main()
