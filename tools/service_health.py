from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ServiceHealthResult:
    service: str
    ok: bool
    latency_ms: int
    detail: str = ""


class OpenAIServiceHealthClient:
    """Read-only health check for an OpenAI-compatible service such as new-api."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds

    async def health(self) -> ServiceHealthResult:
        started = time.perf_counter()
        if not self.base_url:
            return ServiceHealthResult("new-api", False, _elapsed_ms(started), "LLM_BASE_URL 未配置")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(f"{self.base_url}/models", headers=headers)
            response.raise_for_status()
            return ServiceHealthResult("new-api", True, _elapsed_ms(started), "models reachable")
        except httpx.HTTPStatusError as exc:
            return ServiceHealthResult(
                "new-api",
                False,
                _elapsed_ms(started),
                f"HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError as exc:
            return ServiceHealthResult("new-api", False, _elapsed_ms(started), str(exc))


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))
