"""Ordered multi-provider routing for the LLM contract.

The router sits above individual OpenAI-compatible clients. Each client owns
its transport retry budget and circuit; the router only decides whether a
different provider can safely answer the same model request. LLM generation
happens before tool execution, so falling back here never replays a tool with
side effects.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.log import get_logger
from llm.base import LLMClient
from llm.openai_compat import (
    LLMError,
    LLMRequestError,
    LLMUnavailableError,
    OpenAICompatClient,
)
from settings import LLMProviderSettings

log = get_logger("llm.router")


class LLMProviderRouter:
    """Try configured providers in order, preferring the last healthy one."""

    def __init__(self, providers: Sequence[tuple[str, LLMClient]]) -> None:
        if not providers:
            raise ValueError("at least one LLM provider is required")
        self._providers: dict[str, LLMClient] = {}
        self._order: list[str] = []
        for name, client in providers:
            normalized = name.strip().lower()
            if not normalized or normalized in self._providers:
                continue
            self._providers[normalized] = client
            self._order.append(normalized)
        if not self._order:
            raise ValueError("at least one named LLM provider is required")
        self._active_provider = self._order[0]

    @classmethod
    def from_settings(
        cls,
        providers: Sequence[LLMProviderSettings],
    ) -> "LLMProviderRouter":
        clients = [
            (
                config.name,
                OpenAICompatClient(
                    base_url=config.base_url,
                    api_key=config.api_key,
                    model=config.model,
                    timeout_seconds=config.timeout_seconds,
                    max_retries=config.max_retries,
                    failure_threshold=config.failure_threshold,
                    cooldown_seconds=config.cooldown_seconds,
                    quota_cooldown_seconds=config.quota_cooldown_seconds,
                    provider_name=config.name,
                ),
            )
            for config in providers
        ]
        return cls(clients)

    @property
    def active_provider(self) -> str:
        return self._active_provider

    @property
    def model(self) -> str:
        client = self._providers[self._active_provider]
        return str(getattr(client, "model", ""))

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(self._order)

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return await self._invoke("generate", system_prompt, task_prompt)

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        return await self._invoke("repair", raw_output, error, attempt)

    async def _invoke(self, method_name: str, *args: Any) -> str:
        errors: list[LLMError] = []
        for provider_name in self._candidates():
            client = self._providers[provider_name]
            try:
                result = await getattr(client, method_name)(*args)
            except LLMUnavailableError as exc:
                errors.append(exc)
                log.warning(
                    "llm_provider_unavailable",
                    provider=provider_name,
                    operation=method_name,
                    kind=exc.kind,
                )
                continue
            except LLMRequestError as exc:
                # An invalid prompt/model request is shared by all providers.
                # Authentication/configuration errors are provider-local and
                # can safely fall through to the next configured provider.
                if exc.kind != "auth":
                    raise
                errors.append(exc)
                log.warning(
                    "llm_provider_auth_failed",
                    provider=provider_name,
                    operation=method_name,
                )
                continue
            self._active_provider = provider_name
            return result

        if errors and all(isinstance(error, LLMRequestError) for error in errors):
            raise errors[-1]
        detail = "; ".join(
            f"{getattr(error, 'provider', '') or 'provider'}:{getattr(error, 'kind', 'error')}"
            for error in errors
        )
        raise LLMUnavailableError(
            f"all configured LLM providers unavailable ({detail or 'no provider response'})",
            kind="all_providers",
            provider=",".join(self._order),
        ) from (errors[-1] if errors else None)

    def _candidates(self) -> list[str]:
        if self._active_provider not in self._order:
            return list(self._order)
        index = self._order.index(self._active_provider)
        return self._order[index:] + self._order[:index]
