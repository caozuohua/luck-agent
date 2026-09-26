"""Resolve unambiguous local inventory questions from live configuration."""
from __future__ import annotations

import re

from tools.registry import ToolRegistry


# Full match intentionally excludes compound requests such as 'list tools,
# then search the web'. Unknown phrasing still goes through the normal planner,
# which receives the same live local availability in each tool description.
_LOCAL_INVENTORY = re.compile(
    r"(?:请)?(?:列出|查看|告诉我)?(?:你|本机|当前|现在|这里|luck-agent)?"
    r"(?:当前|现在)?(?:有|支持|能用)?(?:哪些|什么|的)?"
    r"(?:工具(?:[、,，]技能[、,，]mcp)?|能力)"
    r"(?:可用|列表|清单)?(?:的|吗)?[？?。！!]*",
    re.IGNORECASE,
)


def local_inventory_answer(text: str, registry: ToolRegistry) -> str | None:
    normalized = re.sub(r"\s+", "", text).strip()
    if not _LOCAL_INVENTORY.fullmatch(normalized):
        return None
    lines = ["当前自然语言执行链的工具注册表："]
    for entry in registry.capability_snapshot():
        state = "本地配置就绪" if entry["locally_ready"] else f"不可用（{entry['reason']}）"
        lines.append(f"• {entry['name']}：{state}")
    if not registry.names():
        lines.append("• 尚未注册工具")
    lines.append("配置就绪不等于已获操作授权或远端服务健康。")
    lines.append("此清单不覆盖快捷运维、技能或 MCP；它们未在此注册表中验证，不能据此声称可用。")
    return "\n".join(lines)
