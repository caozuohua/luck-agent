"""In-memory fake LLM client for offline runs and tests.

When no ``LLM_BASE_URL`` is configured, `main.py` wires this client in so the
runtime can still boot and exercise every code path (routing, goal lifecycle,
memory, notifications) without a model backend. It returns deterministic,
schema-valid output so tool-call flows and JSON repair stay testable.
"""
from __future__ import annotations

import json

from core.output_parser import IntentType

_FAKE_MODEL = "fake-llm"


def _fake_action(tool_name: str, args: dict, plan: str = "fake plan") -> str:
    """Return a valid ACTION intent JSON string."""
    return json.dumps(
        {
            "intent": IntentType.ACTION.value,
            "plan": plan,
            "fallback": "",
            "tool_call": {"name": tool_name, "args": args},
        },
        ensure_ascii=False,
    )


class FakeLLMClient:
    """Deterministic stand-in for a real LLM.

    Classification defaults to CHAT unless overridden. ``queue`` lets a test
    drive a precise sequence of raw outputs (e.g. an ACTION then a CHAT).
    """

    def __init__(
        self,
        *,
        default_intent: IntentType = IntentType.CHAT,
        queue: list[str] | None = None,
        model: str = _FAKE_MODEL,
    ) -> None:
        self.model = model
        self.default_intent = default_intent
        self._queue = list(queue or [])
        self.calls: list[tuple[str, str]] = []

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        self.calls.append((system_prompt, task_prompt))
        if self._queue:
            return self._queue.pop(0)
        if self.default_intent is IntentType.CHAT:
            return json.dumps(
                {"intent": IntentType.CHAT.value, "message": "（fake reply）"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"intent": IntentType.CANNOT_COMPLETE.value, "reason": "fake",
             "suggestion": "retry"},
            ensure_ascii=False,
        )

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        # Repair by returning a well-formed CHAT echo of the parse error.
        return json.dumps(
            {
                "intent": IntentType.CHAT.value,
                "message": f"[repaired] {error}",
            },
            ensure_ascii=False,
        )
