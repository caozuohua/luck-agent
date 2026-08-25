from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryProposal:
    """A candidate memory that still requires an explicit user save action."""

    content: str
    reason: str

    @property
    def save_command(self) -> str:
        return f"/mem0 save {self.content}"


class MemoryProposalDetector:
    """Detect explicit memory intent without calling an LLM or Mem0."""

    _COMMAND_RE = re.compile(r"^(?:/|mem0\b)", re.IGNORECASE)
    _RULES: tuple[tuple[re.Pattern[str], str], ...] = (
        (
            re.compile(
                r"^(?:请|请你)?(?:记住|记下来|长期记住|保存)(?:一下)?(?:[:：,，\s]*)(?P<content>.+)$",
                re.IGNORECASE,
            ),
            "检测到明确的记忆请求",
        ),
        (
            re.compile(r"^(?:please\s+)?remember(?:\s+that)?\s+(?P<content>.+)$", re.IGNORECASE),
            "detected an explicit memory request",
        ),
        (
            re.compile(r"^(?:我|我的).{0,16}(?:喜欢|偏好|习惯|通常|默认).+$"),
            "检测到可能的个人偏好",
        ),
        (
            re.compile(r"^(?:i\s+(?:prefer|like|usually\s+use)|my\s+preference\s+is)\s+.+$", re.IGNORECASE),
            "detected a possible personal preference",
        ),
    )

    def __init__(self, *, max_chars: int = 500) -> None:
        self.max_chars = max(80, max_chars)

    def detect(self, text: str) -> MemoryProposal | None:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized or self._COMMAND_RE.search(normalized):
            return None
        for rule, reason in self._RULES:
            match = rule.search(normalized)
            if not match:
                continue
            content = str(match.groupdict().get("content") or normalized).strip()
            if not content:
                return None
            return MemoryProposal(content=content[: self.max_chars], reason=reason)
        return None
