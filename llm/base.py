"""LLM client protocol for the V2 runtime.

The runtime only depends on this small contract — nothing else references a
specific provider. `VertexClient` (Google Vertex AI) was removed; the runtime
now uses either the OpenAI-compatible HTTP client (`openai_compat.py`) or a
`FakeLLMClient` for offline/test environments.
"""
from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    """Minimal LLM contract used by the V2 runtime.

    Implementations must accept a system prompt and a task prompt and return
    the raw model text. ``repair`` is used by the output parser to ask the
    model to fix malformed tool-call JSON.
    """

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        """Generate text from the model for the given prompts."""
        ...

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        """Ask the model to repair invalid agent JSON output."""
        ...
