from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.message import parse_note_message


class ParseNoteMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_plain_note_defaults_to_idea(self) -> None:
        self.assertEqual(parse_note_message("# 做个记录"), ("做个记录", "idea", []))

    def test_explicit_question_type(self) -> None:
        self.assertEqual(parse_note_message("# [question] 这个怎么做"), ("这个怎么做", "question", []))

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

    def test_pkb_action_url_overrides_default(self) -> None:
        from handlers.message import _pkb_url

        with patch.dict(
            "os.environ",
            {
                "VERCEL_API_URL": "https://example.com/api/pkb",
                "PKB_SEARCH_URL": "https://example.com/api/search",
                "PKB_INGEST_URL": "https://example.com/api/notes",
            },
            clear=False,
        ):
            self.assertEqual(_pkb_url("search"), "https://example.com/api/search")
            self.assertEqual(_pkb_url("ingest"), "https://example.com/api/notes")
            self.assertEqual(_pkb_url("default"), "https://example.com/api/pkb")

    def test_pkb_error_detail_prefers_json_error(self) -> None:
        from handlers.message import _pkb_error_detail

        resp = SimpleNamespace(
            json=lambda: {"error": "Failed to create note"},
            text='{"error":"Failed to create note"}',
        )
        self.assertEqual(_pkb_error_detail(resp), "Failed to create note")

    async def test_forward_to_pkb_result_tolerates_non_object_success_json(self) -> None:
        from handlers import message

        resp = SimpleNamespace(
            is_success=True,
            status_code=200,
            json=lambda: ["created"],
        )
        with (
            patch("handlers.message._pkb_env", return_value=("https://example.com/api/notes", "secret")),
            patch("handlers.message._pkb_post", new=AsyncMock(return_value=resp)),
            patch.dict("os.environ", {"PKB_INGEST_URL": "https://example.com/api/notes"}, clear=False),
        ):
            result = await message.forward_to_pkb_result("note", "idea", ["Python"])

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["type"], "idea")
        self.assertEqual(result["topics"], ["Python"])

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
