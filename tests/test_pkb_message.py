from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from handlers.message import (
    check_pkb_health,
    forward_to_pkb_result,
    parse_note_message,
    search_pkb,
)
from agent import _pkb_record_detail


class ParseNoteMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_pkb_record_detail_distinguishes_idempotent_save(self) -> None:
        self.assertEqual(
            _pkb_record_detail({"ok": True, "idempotent": True}),
            "知识库中已有该内容",
        )

    def test_pkb_record_detail_reports_new_save(self) -> None:
        self.assertEqual(
            _pkb_record_detail({"ok": True, "idempotent": False}),
            "已保存到个人知识库",
        )

    def test_pkb_record_detail_reports_actual_error(self) -> None:
        self.assertEqual(
            _pkb_record_detail({"ok": False, "error": "PKB 认证失败"}),
            "PKB 认证失败",
        )

    def test_plain_note_defaults_to_idea(self) -> None:
        self.assertEqual(parse_note_message("# 做个记录"), ("做个记录", "idea", []))

    def test_explicit_question_type(self) -> None:
        self.assertEqual(parse_note_message("# [question] 这个怎么做"), ("这个怎么做", "question", []))

    def test_note_parser_accepts_all_stable_types(self) -> None:
        for note_type in ("fact", "idea", "task", "question", "code"):
            with self.subTest(note_type=note_type):
                parsed = parse_note_message(f"# [{note_type}] content")
                self.assertEqual(parsed[1], note_type)

    def test_topics_and_fact_type(self) -> None:
        self.assertEqual(
            parse_note_message("# [fact] #Python #AI 这里是一条事实"),
            ("这里是一条事实", "fact", ["Python", "AI"]),
        )

    def test_topics_are_case_normalized_and_deduped(self) -> None:
        self.assertEqual(
            parse_note_message("# [fact] #python #Python #ai #AI 这里是一条事实"),
            ("这里是一条事实", "fact", ["Python", "AI"]),
        )

    def test_invalid_type_falls_back_to_idea(self) -> None:
        self.assertEqual(
            parse_note_message("# [unknown] #机器学习 继续记录"),
            ("继续记录", "idea", ["机器学习"]),
        )

    def test_non_note_returns_none(self) -> None:
        self.assertIsNone(parse_note_message("普通文本"))

    def test_invalid_limit_coerces_in_search_helper(self) -> None:
        from handlers.message import _coerce_pkb_limit

        self.assertEqual(_coerce_pkb_limit("7"), 7)
        self.assertEqual(_coerce_pkb_limit("bad"), 5)
        self.assertEqual(_coerce_pkb_limit(None), 5)
        self.assertEqual(_coerce_pkb_limit(99), 10)

    def test_normalize_pkb_payload_accepts_common_shapes(self) -> None:
        from handlers.message import _normalize_pkb_result_payload

        summary, results = _normalize_pkb_result_payload(
            {
                "answer": "这是摘要",
                "records": [
                    {"title": "记录 1", "content": "第一条"},
                    {"text": "第二条"},
                ],
            }
        )
        self.assertEqual(summary, "这是摘要")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "记录 1")
        self.assertEqual(results[1]["content"], "第二条")

        summary2, results2 = _normalize_pkb_result_payload(
            {
                "notes": [
                    {"title": "记录 2", "content": "第三条"},
                ],
                "hits": [],
            }
        )
        self.assertEqual(summary2, "")
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0]["title"], "记录 2")

    async def test_forward_to_pkb_result_uses_luck_agent_source(self) -> None:
        client = AsyncMock()
        client.save.return_value = {
            "ok": True,
            "id": "n1",
            "type": "fact",
            "topics": ["Python"],
            "idempotent": False,
        }
        with patch("handlers.message.get_pkb_client", return_value=client):
            result = await forward_to_pkb_result("note", "fact", ["Python"])

        client.save.assert_awaited_once_with(
            "note",
            source="luck-agent",
            note_type="fact",
            topics=["Python"],
        )
        self.assertFalse(result["idempotent"])

    async def test_search_pkb_omits_source_by_default(self) -> None:
        client = AsyncMock()
        client.search.return_value = {"ok": True, "results": [], "count": 0}
        with patch("handlers.message.get_pkb_client", return_value=client):
            result = await search_pkb("Python", limit=5)

        client.search.assert_awaited_once_with("Python", limit=5)
        self.assertEqual(result["query"], "Python")

    async def test_check_pkb_health_delegates_to_client(self) -> None:
        client = AsyncMock()
        client.health.return_value = {"ok": True, "status": "ok"}
        with patch("handlers.message.get_pkb_client", return_value=client):
            result = await check_pkb_health()

        client.health.assert_awaited_once_with()
        self.assertEqual(result["status"], "ok")

    def test_format_pkb_result_items_normalizes_topics_and_includes_url(self) -> None:
        from handlers.message import format_pkb_result_items

        lines = format_pkb_result_items(
            [
                {
                    "title": "Python note",
                    "type": "idea",
                    "topics": "python, Python, AI",
                    "content": "Python content",
                    "url": "https://example.com/note",
                }
            ],
            limit=1,
        )

        self.assertEqual(
            lines,
            [
                "- [idea · Python / AI] **Python note**",
                "  Python content",
                "  🔗 https://example.com/note",
            ],
        )

    def test_search_pkb_tool_summary_uses_consistent_item_format(self) -> None:
        from handlers.message import AgentMessageHandler

        handler = AgentMessageHandler.__new__(AgentMessageHandler)
        text = handler._summarize_tool_results(
            [
                {
                    "tool": "search_pkb",
                    "result": {
                        "summary": "找到 1 条",
                        "results": [
                            {
                                "title": "Python note",
                                "type": "idea",
                                "topics": "python, Python, AI",
                                "content": "Python content",
                                "url": "https://example.com/note",
                            }
                        ],
                    },
                }
            ]
        )

        self.assertIn("🗃️ 个人知识库检索完成：找到 1 条", text)
        self.assertIn("- [idea · Python / AI] **Python note**", text)
        self.assertIn("  🔗 https://example.com/note", text)


if __name__ == "__main__":
    unittest.main()
