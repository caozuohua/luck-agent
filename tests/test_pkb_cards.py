from __future__ import annotations

import unittest

from cards.builder import CardBuilder


class PkbCardTests(unittest.TestCase):
    def test_pkb_results_card(self) -> None:
        card = CardBuilder.pkb_results(
            "Python",
            {
                "count": 2,
                "summary": "查到两条相关笔记",
                "results": [
                    {
                        "title": "Python 速记",
                        "type": "fact",
                        "topics": ["python", "Python", "ai", "AI"],
                        "content": "这是第一条笔记内容",
                        "url": "https://example.com/1",
                    },
                ],
            },
        )
        self.assertEqual(card["header"]["title"]["content"], "🗃️ 个人知识库")
        body = card["body"]["elements"]
        self.assertIn("Python", body[0]["fields"][0]["text"]["content"])
        self.assertTrue(any("查到两条相关笔记" in str(elem) for elem in body))
        self.assertTrue(any("Python 速记" in str(elem) for elem in body))
        self.assertTrue(any("fact · Python / AI" in str(elem) for elem in body))
        self.assertTrue(any("[打开原文](https://example.com/1)" in str(elem) for elem in body))
        self.assertFalse(
            any(
                "https://example.com/1" in str(elem) and "这是第一条笔记内容" in str(elem)
                for elem in body
            )
        )

    def test_pkb_recorded_card(self) -> None:
        card = CardBuilder.pkb_recorded(
            "这是一次录入",
            "idea",
            ["Python"],
            ok=True,
            detail="已转发到个人知识库",
        )
        self.assertEqual(card["header"]["title"]["content"], "🗃️ 个人知识库录入")
        body = card["body"]["elements"]
        self.assertIn("已记录", body[0]["fields"][0]["text"]["content"])
        self.assertTrue(any("这是一次录入" in str(elem) for elem in body))
        self.assertTrue(any("已转发到个人知识库" in str(elem) for elem in body))

    def test_pkb_recorded_failure_card(self) -> None:
        card = CardBuilder.pkb_recorded(
            "这条会失败",
            "question",
            [],
            ok=False,
            detail="接口返回错误",
        )
        self.assertEqual(card["header"]["template"], "red")
        body = card["body"]["elements"]
        self.assertIn("记录失败", body[0]["fields"][0]["text"]["content"])
        self.assertTrue(any("接口返回错误" in str(elem) for elem in body))


if __name__ == "__main__":
    unittest.main()
