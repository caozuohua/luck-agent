"""OpenAI-compatible HTTP LLM client for the V2 runtime.

Replaces the removed Vertex AI client. Targets any OpenAI-compatible
``/chat/completions`` endpoint, so it works with OpenRouter, Nous/ModelRoute,
the Hermes proxy, OpenAI directly, or a local model server (llama.cpp,
vLLM, Ollama with the OpenAI shim). Auth is a single bearer API key.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from core.log import get_logger

log = get_logger("llm.openai_compat")


class LLMError(RuntimeError):
    """Base error for an OpenAI-compatible provider failure."""


class LLMUnavailableError(LLMError):
    """The provider is temporarily unavailable or its circuit is open."""


class LLMRequestError(LLMError):
    """The request is invalid or authentication is not accepted."""


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
        max_retries: int = 2,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        max_backoff_seconds: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.max_retries = max(0, max_retries)
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(1.0, cooldown_seconds)
        self.max_backoff_seconds = max(0.1, max_backoff_seconds)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        if not self.base_url:
            raise ValueError("base_url is required for OpenAICompatClient")

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        self._ensure_available()
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
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    if resp.status_code in {408, 409, 425, 429} or resp.status_code >= 500:
                        last_err = self._transient_error(resp)
                        if attempt < self.max_retries:
                            await asyncio.sleep(self._backoff(attempt, resp))
                            continue
                        self._record_failure()
                        raise LLMUnavailableError(str(last_err)) from last_err
                    if resp.status_code >= 400:
                        try:
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise LLMRequestError(
                                f"LLM request rejected ({resp.status_code})"
                            ) from exc
                        raise LLMRequestError(f"LLM request rejected ({resp.status_code})")
                    body = resp.json()
                    text = self._extract_text(body)
                    if not text:
                        last_err = RuntimeError("LLM returned an empty response")
                        if attempt < self.max_retries:
                            await asyncio.sleep(self._backoff(attempt))
                            continue
                        self._record_failure()
                        raise LLMUnavailableError(str(last_err)) from last_err
                    self._record_success()
                    log.debug("llm_generated", model=self.model, chars=len(text))
                    return text
                except LLMRequestError:
                    raise
                except LLMUnavailableError:
                    raise
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_err = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    self._record_failure()
                    raise LLMUnavailableError(f"LLM transport failure: {exc}") from exc
                except (ValueError, TypeError) as exc:
                    last_err = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    self._record_failure()
                    raise LLMUnavailableError(f"LLM response invalid: {exc}") from exc
        raise LLMUnavailableError(str(last_err or "LLM generate failed"))

    def _ensure_available(self) -> None:
        if self._circuit_open_until <= 0:
            return
        now = time.monotonic()
        if now < self._circuit_open_until:
            remaining = max(1, int(self._circuit_open_until - now))
            raise LLMUnavailableError(f"LLM circuit open; retry in {remaining}s")
        self._circuit_open_until = 0.0
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._circuit_open_until = time.monotonic() + self.cooldown_seconds
            log.warning(
                "llm_circuit_open",
                model=self.model,
                cooldown_seconds=self.cooldown_seconds,
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        retry_after = ""
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
        try:
            if retry_after:
                return min(self.max_backoff_seconds, max(0.1, float(retry_after)))
        except ValueError:
            pass
        return min(self.max_backoff_seconds, 0.5 * (2**attempt))

    def _transient_error(self, response: httpx.Response) -> RuntimeError:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("error") or body.get("message") or "")
        except (ValueError, TypeError):
            detail = response.text[:160]
        suffix = f": {detail[:160]}" if detail else ""
        if response.status_code == 429:
            return RuntimeError(f"429 rate limited{suffix}")
        return RuntimeError(f"LLM provider HTTP {response.status_code}{suffix}")

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
