from __future__ import annotations

from typing import Any

from core.services import ServiceSpec
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


def build_sections_card(
    sections: list[str],
    *,
    title: str = "Luck Agent",
) -> dict[str, Any]:
    """Build a Card 2.0 result with separately scannable Markdown sections."""
    content_sections = [str(section or "（无返回内容）").strip() for section in sections]
    content_sections = [section for section in content_sections if section]
    if not content_sections:
        content_sections = ["（无返回内容）"]
    card = build_text_card("\n\n".join(content_sections), title=title)
    card["body"]["elements"] = [
        {"tag": "markdown", "content": section} for section in content_sections
    ]
    return card


def build_assistant_result_card(text: str) -> dict[str, Any]:
    """Build the default card for natural-language or LLM responses."""
    content = str(text or "（无返回内容）").strip()
    return build_sections_card(
        _chunk_markdown(content),
        title="Luck Agent",
    )


def build_memory_proposal_card(content: str, *, reason: str = "") -> dict[str, Any]:
    """Build a proposal card whose button only starts the approval flow."""
    card = build_sections_card(
        [
            "🧠 检测到可能需要长期记忆的信息，但当前不会自动保存。",
            f"• 候选内容：{content}",
            f"• 原因：{reason}" if reason else "",
            "点击按钮后会生成一次性确认码；确认前不会写入 Mem0。",
        ],
        title="Luck Agent · 记忆提议",
    )
    card["body"]["elements"].append(
        {
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "发起保存确认"},
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "action": "memory_save_proposal",
                                        "content": content,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    return card


def build_approval_card(
    request: str,
    *,
    token: str,
    ttl_seconds: float,
) -> dict[str, Any]:
    """Build a one-click confirmation card with a text fallback."""
    minutes = max(1, int(ttl_seconds // 60))
    card = build_sections_card(
        [
            "⚠️ 该请求可能修改系统或数据，暂未执行。\n"
            f"• 文字确认：`/confirm {token}`",
            f"• 请求：{str(request or '').strip()}",
            "点击「确认执行」即可继续；也可使用下方验证码文字确认。",
            f"• 有效期：{minutes} 分钟",
        ],
        title="Luck Agent · 操作确认",
    )
    card["body"]["elements"].append(
        {
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "确认执行"},
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "action": "approval_confirm",
                                        "token": token,
                                    },
                                }
                            ],
                        }
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "type": "default",
                            "text": {"tag": "plain_text", "content": "取消"},
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {"action": "approval_cancel"},
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    )
    return card


def build_service_catalog_card(specs: list[ServiceSpec]) -> dict[str, Any]:
    """Build a compact, one-service-per-section catalog card."""
    if not specs:
        return build_sections_card(
            ["🧩 当前没有已授权的服务"],
            title="Luck Agent · 服务目录",
        )
    sections = ["🧩 **可用服务**"]
    sections.extend(
        f"**`{spec.service_id}` · {spec.label}**\n{spec.description}"
        for spec in specs
    )
    sections.append("用法：`/vps service SERVICE status|smoke|search 关键词`")
    return build_sections_card(sections, title="Luck Agent · 服务目录")


def build_goal_result_card(
    *,
    goal_id: str,
    status: str,
    result: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Build a structured terminal card for a background Goal."""
    normalized_status = str(status or "").upper()
    completed = normalized_status == "DONE"
    mark = "✅" if completed else "❌"
    title = "Luck Agent · 任务完成" if completed else "Luck Agent · 任务失败"
    heading = f"{mark} 后台任务{'完成' if completed else '失败'}"
    detail = str(result if completed else (error or result) or "请检查任务状态。").strip()
    return build_sections_card(
        [heading, f"• Goal：`{str(goal_id or '')[:8]}`", detail],
        title=title,
    )


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


def build_log_page_card(
    text: str,
    *,
    page: int,
    total_pages: int,
    token: str,
) -> dict[str, Any]:
    """Build a Card 2.0 log page with short-lived callback navigation."""
    return build_output_page_card(
        text,
        page=page,
        total_pages=total_pages,
        token=token,
        action="vps_logs_page",
        heading="服务日志",
    )


def build_output_page_card(
    text: str,
    *,
    page: int,
    total_pages: int,
    token: str,
    action: str = "vps_output_page",
    heading: str = "服务输出",
) -> dict[str, Any]:
    """Build a Card 2.0 page for any bounded command output."""
    card = build_text_card(
        text,
        title=f"Luck Agent · {heading} {page}/{total_pages}",
    )
    buttons: list[dict[str, Any]] = []
    if page > 1:
        buttons.append(
            _output_page_button(
                "上一页",
                page=page - 1,
                token=token,
                action=action,
            )
        )
    if page < total_pages:
        buttons.append(
            _output_page_button(
                "下一页",
                page=page + 1,
                token=token,
                action=action,
                primary=True,
            )
        )
    if buttons:
        card["body"]["elements"].append(
            {
                "tag": "column_set",
                "flex_mode": "bisect",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [button],
                    }
                    for button in buttons
                ],
            }
        )
    return card


def _output_page_button(
    label: str,
    *,
    page: int,
    token: str,
    action: str,
    primary: bool = False,
) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "primary" if primary else "default",
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": action,
                    "page": str(page),
                    "token": token,
                },
            }
        ],
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


def _chunk_markdown(content: str, *, limit: int = 1800) -> list[str]:
    """Split long Markdown at paragraph boundaries without dropping content."""
    if len(content) <= limit:
        return [content]
    paragraphs = content.split("\n\n")
    sections: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            sections.append(current)
            current = ""
        while len(paragraph) > limit:
            sections.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        sections.append(current)
    return sections or [content[:limit]]
