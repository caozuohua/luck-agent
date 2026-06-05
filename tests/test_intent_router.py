from __future__ import annotations

import unittest

from core.intent_router import Intent, route


class IntentRouterTests(unittest.TestCase):
    def test_pkb_write_routes_before_search(self) -> None:
        result = route("把这个结论记到知识库：SQLite 日志要批量写入")

        self.assertEqual(result.intent, Intent.PKB_WRITE)
        self.assertEqual(result.tool_names, ["write_pkb"])
        self.assertIn("write_pkb", result.prompt_hint)


if __name__ == "__main__":
    unittest.main()
