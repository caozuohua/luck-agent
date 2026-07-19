"""OpenAI-compatible HTTP LLM client for the V2 runtime.

Replaces the removed Vertex AI client. Targets any OpenAI-compatible
``/chat/completions`` endpoint, so it works with OpenRouter, Nous/ModelRoute,
the Hermes proxy, OpenAI directly, or a local model server (llama.cpp,
vLLM, Ollama with the OpenAI shim). Auth is a single bearer API key.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from core.log import get_logger

log = get_logger("llm.openai_compat")


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    return stripped


class OpenAICompatClient:
    """Talk to an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout_seconds: float = 60.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        if not self.base_url:
            raise ValueError("base_url is required for OpenAICompatClient")

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
        text = self._extract_text(body)
        log.debug("llm_generated", model=self.model, chars=len(text))
        return text

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        system_prompt = (
            "You repair invalid agent JSON. Return only one valid JSON object "
            "matching ACTION, CHAT, CLARIFY, or CANNOT_COMPLETE schema."
        )
        task_prompt = (
            f"Attempt: {attempt}\n"
            f"Parse error: {error}\n"
            f"Invalid output:\n{raw_output}"
        )
        return await self.generate(system_prompt, task_prompt)

    def _extract_text(self, body: dict[str, Any]) -> str:
        parts: list[str] = []
        for choice in body.get("choices", []):
            message = choice.get("message") or {}
            content = message.get("content")
            if content:
                parts.append(str(content))
        return _strip_json_fence("".join(parts)).strip()
