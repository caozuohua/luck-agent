from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tools.search_tools import SearchTools


class SearchToolsTests(unittest.TestCase):
    def test_configures_two_direct_tavily_api_keys(self) -> None:
        with patch.dict(
            os.environ,
            {"TAVILY_API_KEY": "key-one", "TAVILY_API_KEY_2": "key-two"},
            clear=False,
        ):
            searcher = SearchTools()

        self.assertEqual(
            searcher._tavily_backends,
            [
                {"name": "tavily-1", "url": "https://api.tavily.com/search", "key": "key-one"},
                {"name": "tavily-2", "url": "https://api.tavily.com/search", "key": "key-two"},
            ],
        )

    def test_tavily_results_omit_ai_answer_and_keep_detailed_content(self) -> None:
        searcher = SearchTools()
        content = "x" * 900

        result = searcher._format_tavily_result(
            {
                "answer": "AI summary must not be shown",
                "results": [
                    {"title": "Result", "url": "https://example.com", "content": content}
                ],
            }
        )

        self.assertNotIn("summary", result)
        self.assertEqual(result["results"][0]["description"], content)
