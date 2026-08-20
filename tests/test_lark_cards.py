from __future__ import annotations

from core.targets import VpsTarget
from interface.lark_cards import build_target_selection_card, build_text_card


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


def test_target_selection_card_uses_callback_safe_object_values() -> None:
    card = build_target_selection_card(
        [VpsTarget(provider="aws", target_id="aws-01")],
    )

    selector = card["body"]["elements"][1]
    assert selector["tag"] == "select_static"
    assert selector["name"] == "target_select"
    assert selector["options"][0]["value"] == {"target_id": "aws-01"}
