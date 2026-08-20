from __future__ import annotations

from typing import Any

from core.targets import VpsTarget


def build_text_card(text: str, *, title: str = "Luck Agent") -> dict[str, Any]:
    """Build a mobile-friendly Card 2.0 text result.

    Keep the first body element as Markdown for compatibility with existing
    senders and tests, while adding a real Card 2.0 header for scanning on
    mobile clients.
    """
    content = str(text or "（无返回内容）").strip()
    heading = _heading(content)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": False},
        "header": {
            "title": {"tag": "plain_text", "content": _truncate(f"{title} · {heading}")},
            "template": _template(content),
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": content},
            ]
        },
    }


def build_target_selection_card(
    targets: list[VpsTarget],
    *,
    current: VpsTarget | None = None,
) -> dict[str, Any]:
    options = [
        {
            "text": {"tag": "plain_text", "content": target.display},
            # Card 2.0 select_static options require a string value. The
            # callback handler also accepts the SDK's `option` field.
            "value": target.label,
        }
        for target in targets
    ]
    current_text = current.display if current is not None else "未选择"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": False},
        "header": {
            "title": {"tag": "plain_text", "content": "Luck Agent · 选择 VPS 目标"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"当前目标：**{current_text}**\n请选择后续运维目标："},
                {
                    "tag": "select_static",
                    "name": "target_select",
                    "placeholder": {"tag": "plain_text", "content": "选择目标"},
                    "options": options,
                },
            ]
        },
    }


def _heading(content: str) -> str:
    first = next((line.strip() for line in content.splitlines() if line.strip()), "结果")
    first = first.lstrip("✅❌⚠️🏓🩺🖥️🧠 ")
    return _truncate(first.split("：", 1)[0].split(":", 1)[0] or "结果", 24)


def _template(content: str) -> str:
    if any(mark in content for mark in ("❌", "失败", "错误", "拒绝")):
        return "red"
    if any(mark in content for mark in ("⚠️", "警告", "待确认")):
        return "orange"
    if any(mark in content for mark in ("✅", "正常", "完成")):
        return "green"
    return "blue"


def _truncate(value: str, limit: int = 60) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"
