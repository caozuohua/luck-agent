from __future__ import annotations

from interface.lark_cards import build_text_card


def test_card_has_mobile_header_and_compatible_markdown_body() -> None:
    card = build_text_card("🖥️ VPS 状态：✅ 正常\n• 主机：`aws-prod`")

    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"].startswith("Luck Agent · VPS 状态")
    assert card["header"]["template"] == "green"
    assert card["body"]["elements"][0]["tag"] == "markdown"
    assert "aws-prod" in card["body"]["elements"][0]["content"]


def test_card_template_reflects_failure_or_confirmation() -> None:
    assert build_text_card("❌ 操作失败")["header"]["template"] == "red"
    assert build_text_card("⚠️ 待确认") ["header"]["template"] == "orange"
