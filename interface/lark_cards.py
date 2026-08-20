from __future__ import annotations

from typing import Any


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
