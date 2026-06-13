from __future__ import annotations

import unittest

from core.intent_router import Intent, route


class IntentRouterTests(unittest.TestCase):
    def test_pkb_write_routes_before_search(self) -> None:
        result = route("把这个结论记到知识库：SQLite 日志要批量写入")

        self.assertEqual(result.intent, Intent.PKB_WRITE)
        self.assertEqual(result.tool_names, ["pkb_save"])
        self.assertIn("pkb_save", result.prompt_hint)
        self.assertIn("密码", result.prompt_hint)

    def test_pkb_search_exposes_read_tools_and_history_policy(self) -> None:
        result = route("查一下以前关于 Python 的记录")

        self.assertEqual(result.intent, Intent.PKB_SEARCH)
        self.assertEqual(result.tool_names, ["pkb_search", "pkb_get", "pkb_list"])
        self.assertIn("不要传 source", result.prompt_hint)
        self.assertIn("主动先检索", result.prompt_hint)

    def test_pkb_list_update_delete_and_restore_routes(self) -> None:
        listed = route("列出最近的 Python 知识库记录")
        updated = route("把知识库里那条 Python 记录改成新内容")
        deleted = route("删除知识库里那条 Python 记录")
        restored = route("撤销刚才删除的知识库记录")

        self.assertEqual(listed.intent, Intent.PKB_LIST)
        self.assertEqual(updated.intent, Intent.PKB_UPDATE)
        self.assertEqual(deleted.intent, Intent.PKB_DELETE)
        self.assertEqual(restored.intent, Intent.PKB_RESTORE)
        self.assertIn("pkb_list", listed.tool_names)
        self.assertIn("pkb_update", updated.tool_names)
        self.assertIn("确认", deleted.prompt_hint)
        self.assertIn("软删除", deleted.prompt_hint)
        self.assertNotIn("hard=true", deleted.prompt_hint)
        self.assertIn("pkb_restore", restored.tool_names)


if __name__ == "__main__":
    unittest.main()
