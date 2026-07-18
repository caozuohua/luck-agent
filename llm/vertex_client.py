from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account


class VertexClient:
    """Minimal Vertex AI Gemini REST client using service-account JWT auth.

    This client intentionally does not use ADC. A service account JSON key is
    loaded explicitly and exchanged for an OAuth access token via JWT assertion.
    """

    _SCOPE = "https://www.googleapis.com/auth/cloud-platform"

    def __init__(
        self,
        *,
        project: str,
        location: str,
        model: str,
        service_account_key_path: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not project.strip():
            raise ValueError("project is required")
        if not location.strip():
            raise ValueError("location is required")
        if not model.strip():
            raise ValueError("model is required")
        key_path = Path(service_account_key_path)
        if not key_path.exists():
            raise FileNotFoundError(f"service account key not found: {key_path}")

        self.project = project
        self.location = location
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._credentials = service_account.Credentials.from_service_account_file(
            str(key_path),
            scopes=[self._SCOPE],
        )
        self._token_expiry = 0.0

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        """Generate text from Gemini for the Phase 1 prompt contract."""
        token = await self._access_token()
        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": task_prompt}],
                }
            ],
        }
        url = self._endpoint()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        return self._extract_text(body)

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        """Ask Gemini to repair invalid JSON output."""
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

    async def _access_token(self) -> str:
        if self._credentials.token and time.time() < self._token_expiry - 60:
            return self._credentials.token

        def refresh() -> str:
            self._credentials.refresh(Request())
            expiry = self._credentials.expiry
            self._token_expiry = expiry.timestamp() if expiry else time.time() + 300
            return self._credentials.token or ""

        token = await asyncio.to_thread(refresh)
        if not token:
            raise RuntimeError("failed to obtain Vertex access token")
        return token

    def _endpoint(self) -> str:
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/"
            f"projects/{self.project}/locations/{self.location}/"
            f"publishers/google/models/{self.model}:generateContent"
        )

    def _extract_text(self, body: dict[str, Any]) -> str:
        parts: list[str] = []
        for candidate in body.get("candidates", []):
            content = candidate.get("content") or {}
            for part in content.get("parts", []):
                text = part.get("text")
                if text:
                    parts.append(str(text))
        return "".join(parts).strip()
