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

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unavailable",
        status_code: int | None = None,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.provider = provider


class LLMRequestError(LLMError):
    """The request is invalid or authentication is not accepted."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "request",
        status_code: int | None = None,
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.provider = provider


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
        quota_cooldown_seconds: float = 3600.0,
        max_backoff_seconds: float = 8.0,
        provider_name: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider_name = provider_name
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.max_retries = max(0, max_retries)
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(1.0, cooldown_seconds)
        self.quota_cooldown_seconds = max(1.0, quota_cooldown_seconds)
        self.max_backoff_seconds = max(0.1, max_backoff_seconds)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_kind = "unavailable"
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
                        if attempt < self.max_retries and last_err.kind != "quota":
                            await asyncio.sleep(self._backoff(attempt, resp))
                            continue
                        self._record_failure(last_err.kind)
                        raise last_err
                    if resp.status_code >= 400:
                        error = self._request_error(resp)
                        if isinstance(error, LLMUnavailableError):
                            self._record_failure(error.kind)
                        raise error
                    body = resp.json()
                    text = self._extract_text(body)
                    if not text:
                        last_err = LLMUnavailableError(
                            "LLM returned an empty response",
                            kind="empty_response",
                            provider=self.provider_name,
                        )
                        if attempt < self.max_retries:
                            await asyncio.sleep(self._backoff(attempt))
                            continue
                        self._record_failure("empty_response")
                        raise last_err
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
                    self._record_failure("transport")
                    raise LLMUnavailableError(
                        f"LLM transport failure: {exc}",
                        kind="transport",
                        provider=self.provider_name,
                    ) from exc
                except (ValueError, TypeError) as exc:
                    last_err = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(self._backoff(attempt))
                        continue
                    self._record_failure("invalid_response")
                    raise LLMUnavailableError(
                        f"LLM response invalid: {exc}",
                        kind="invalid_response",
                        provider=self.provider_name,
                    ) from exc
        raise LLMUnavailableError(
            str(last_err or "LLM generate failed"),
            kind="unavailable",
            provider=self.provider_name,
        )

    def _ensure_available(self) -> None:
        if self._circuit_open_until <= 0:
            return
        now = time.monotonic()
        if now < self._circuit_open_until:
            remaining = max(1, int(self._circuit_open_until - now))
            raise LLMUnavailableError(
                f"LLM circuit open; retry in {remaining}s",
                kind=self._circuit_kind,
                provider=self.provider_name,
            )
        self._circuit_open_until = 0.0
        self._consecutive_failures = 0
        self._circuit_kind = "unavailable"

    def _record_failure(self, kind: str) -> None:
        if kind == "quota":
            self._consecutive_failures = self.failure_threshold
            self._circuit_kind = kind
            self._circuit_open_until = time.monotonic() + self.quota_cooldown_seconds
            log.warning(
                "llm_provider_quota_circuit_open",
                model=self.model,
                provider=self.provider_name,
                cooldown_seconds=self.quota_cooldown_seconds,
            )
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._circuit_kind = kind
            self._circuit_open_until = time.monotonic() + self.cooldown_seconds
            log.warning(
                "llm_circuit_open",
                model=self.model,
                provider=self.provider_name,
                kind=kind,
                cooldown_seconds=self.cooldown_seconds,
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_kind = "unavailable"

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

    def _transient_error(self, response: httpx.Response) -> LLMUnavailableError:
        detail = ""
        try:
            body = response.json()
            detail = str(body.get("error") or body.get("message") or "")
        except (ValueError, TypeError):
            detail = response.text[:160]
        suffix = f": {detail[:160]}" if detail else ""
        kind = "quota" if self._is_quota_detail(detail, response.status_code) else "transient"
        if response.status_code == 429 and kind != "quota":
            kind = "rate_limit"
        elif response.status_code >= 500:
            kind = "server"
        message = (
            f"429 rate limited{suffix}"
            if response.status_code == 429
            else f"LLM provider HTTP {response.status_code}{suffix}"
        )
        return LLMUnavailableError(
            message,
            kind=kind,
            status_code=response.status_code,
            provider=self.provider_name,
        )

    def _request_error(self, response: httpx.Response) -> LLMError:
        detail = self._response_detail(response)
        if self._is_quota_detail(detail, response.status_code):
            return LLMUnavailableError(
                f"LLM provider quota unavailable ({response.status_code})",
                kind="quota",
                status_code=response.status_code,
                provider=self.provider_name,
            )
        kind = "auth" if response.status_code in {401, 403} else "request"
        return LLMRequestError(
            f"LLM request rejected ({response.status_code})",
            kind=kind,
            status_code=response.status_code,
            provider=self.provider_name,
        )

    def _response_detail(self, response: httpx.Response) -> str:
        try:
            body = response.json()
            return str(body.get("error") or body.get("message") or "")
        except (ValueError, TypeError):
            return response.text[:160]

    def _is_quota_detail(self, detail: str, status_code: int) -> bool:
        if status_code == 402:
            return True
        lowered = detail.lower()
        markers = (
            "insufficient_quota",
            "quota exceeded",
            "quota exhausted",
            "exceeded your current quota",
            "billing",
            "payment required",
            "insufficient credit",
            "credit balance",
            "daily limit",
            "monthly limit",
            "usage limit",
        )
        return any(marker in lowered for marker in markers)

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
