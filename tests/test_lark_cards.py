from __future__ import annotations

from core.targets import VpsTarget
from interface.lark_cards import (
    build_assistant_result_card,
    build_goal_result_card,
    build_log_page_card,
    build_memory_proposal_card,
    build_output_page_card,
    build_sections_card,
    build_service_catalog_card,
    build_target_selection_card,
    build_text_card,
)
from core.services import SERVICE_CATALOG


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


def test_sections_card_keeps_each_result_section_scannable() -> None:
    card = build_sections_card(["**状态**：✅ 正常", "• 延迟：4 ms"], title="Luck Agent · Mem0")

    elements = card["body"]["elements"]
    assert [element["tag"] for element in elements] == ["markdown", "markdown"]
    assert elements[1]["content"] == "• 延迟：4 ms"


def test_memory_proposal_card_has_approval_callback_button() -> None:
    card = build_memory_proposal_card("我偏好简洁回答", reason="偏好")

    button = card["body"]["elements"][-1]["columns"][0]["elements"][0]
    assert button["text"]["content"] == "发起保存确认"
    assert button["behaviors"][0]["value"] == {
        "action": "memory_save_proposal",
        "content": "我偏好简洁回答",
    }


def test_service_catalog_card_contains_one_section_per_service() -> None:
    card = build_service_catalog_card(list(SERVICE_CATALOG))

    assert card["header"]["title"]["content"].startswith("Luck Agent · 服务目录")
    assert len(card["body"]["elements"]) == len(SERVICE_CATALOG) + 2


def test_goal_result_card_distinguishes_done_and_failed_states() -> None:
    done = build_goal_result_card(
        goal_id="goal-123456",
        status="DONE",
        result="服务正常",
    )
    failed = build_goal_result_card(
        goal_id="goal-123456",
        status="FAILED",
        error="目标不可达",
    )

    assert done["header"]["template"] == "green"
    assert failed["header"]["template"] == "red"
    assert done["body"]["elements"][1]["content"] == "• Goal：`goal-123`"
    assert failed["body"]["elements"][2]["content"] == "目标不可达"


def test_assistant_result_card_chunks_long_markdown_without_dropping_tail() -> None:
    content = "结论\n\n" + ("详细结果。" * 500)

    card = build_assistant_result_card(content)
    sections = [element["content"] for element in card["body"]["elements"]]

    assert len(sections) > 1
    assert "结论" in sections[0]
    assert "详细结果。" in sections[-1]
    assert sum(section.count("详细结果。") for section in sections) == 500
