"""
runtime/intent_router.py — Goal Runtime intent routing.

This lightweight router maps high-level user requests to Goal Runtime intents.
It is intentionally separate from the existing core.intent_router so PR-6 can
migrate selected intents gradually without breaking the legacy ReAct path.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeRoute:
    intent: str
    use_goal_runtime: bool
    reason: str = ""


class RuntimeIntentRouter:
    """Small migration router for deciding whether to use Goal Runtime."""

    BLOG_KEYWORDS = (
        "博客",
        "blog",
        "文章",
        "发布文章",
        "重构博客",
        "写一篇",
        "改文章",
    )

    def route(self, text: str) -> RuntimeRoute:
        normalized = (text or "").strip().lower()
        if not normalized:
            return RuntimeRoute(intent="general", use_goal_runtime=False, reason="empty message")

        if any(keyword in normalized for keyword in self.BLOG_KEYWORDS):
            return RuntimeRoute(
                intent="blog_write",
                use_goal_runtime=True,
                reason="blog write/publish keyword matched",
            )

        return RuntimeRoute(intent="general", use_goal_runtime=False, reason="fallback to legacy react")
