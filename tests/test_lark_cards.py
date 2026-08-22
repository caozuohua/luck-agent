from __future__ import annotations

from core.targets import VpsTarget
from interface.lark_cards import (
    build_log_page_card,
    build_output_page_card,
    build_target_selection_card,
    build_text_card,
)


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


def test_target_selection_card_uses_lark_compatible_string_values() -> None:
    card = build_target_selection_card(
        [VpsTarget(provider="aws", target_id="aws-01")],
    )

    selector = card["body"]["elements"][1]
    assert selector["tag"] == "select_static"
    assert selector["name"] == "target_select"
    assert selector["options"][0]["value"] == "aws-01"


def test_log_page_card_has_callback_navigation() -> None:
    card = build_log_page_card("log page", page=2, total_pages=3, token="token")

    columns = card["body"]["elements"][-1]["columns"]
    assert len(columns) == 2
    assert columns[0]["elements"][0]["behaviors"][0]["type"] == "callback"
    assert columns[0]["elements"][0]["behaviors"][0]["value"]["page"] == "1"
    assert columns[1]["elements"][0]["behaviors"][0]["value"]["page"] == "3"


def test_generic_output_page_card_uses_separate_callback_action() -> None:
    card = build_output_page_card("resources page", page=1, total_pages=2, token="token")

    button = card["body"]["elements"][-1]["columns"][0]["elements"][0]
    assert button["behaviors"][0]["value"]["action"] == "vps_output_page"
