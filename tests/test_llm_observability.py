from __future__ import annotations

import pytest

from interface.health import HealthService
from interface.lark_commands import QuickCommandRouter
from llm.openai_compat import OpenAICompatClient
from llm.router import LLMProviderRouter
from memory.db import Database
from memory.goal_store import GoalStore


class _StatusClient:
    def __init__(self, model: str, state: str = "ready") -> None:
        self.model = model
        self.state = state

    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return "ok"

    async def repair(self, raw_output: str, error: Exception, attempt: int) -> str:
        return "ok"

    def status(self) -> dict[str, object]:
        return {
            "model": self.model,
            "state": self.state,
            "cooldown_kind": "quota" if self.state == "cooldown" else "",
            "cooldown_remaining_seconds": 3600 if self.state == "cooldown" else 0,
            "consecutive_failures": 3 if self.state == "cooldown" else 0,
        }


def test_openai_client_status_does_not_expose_credentials() -> None:
    client = OpenAICompatClient(
        base_url="https://provider.example/v1",
        api_key="secret-that-must-not-appear",
        model="model-a",
        provider_name="primary",
    )

    status = client.status()

    assert status == {
        "provider": "primary",
        "model": "model-a",
        "state": "ready",
        "cooldown_kind": "",
        "cooldown_remaining_seconds": 0,
        "consecutive_failures": 0,
    }
    assert "secret-that-must-not-appear" not in str(status)
    assert "provider.example" not in str(status)


@pytest.mark.asyncio
async def test_health_reports_active_provider_and_cooldowns() -> None:
    router = LLMProviderRouter(
        [
            ("primary", _StatusClient("model-a")),
            ("backup", _StatusClient("model-b", state="cooldown")),
        ]
    )
    db = Database(":memory:")
    await db.initialize()
    try:
        payload = await HealthService(
            db=db,
            goal_store=GoalStore(db),
            llm=router,
        ).collect_status()
    finally:
        await db.close()

    assert payload["llm"]["active_provider"] == "primary"
    assert payload["llm"]["providers"][1]["cooldown_kind"] == "quota"
    assert payload["llm"]["providers"][1]["cooldown_remaining_seconds"] == 3600


@pytest.mark.asyncio
async def test_health_command_shows_provider_summary_without_calling_llm() -> None:
    class Health:
        async def collect_status(self) -> dict:
            return {
                "process": {"status": "ok"},
                "sqlite": {"connected": True},
                "goals": {"done": 2, "failed": 0},
                "llm": {
                    "active_provider": "backup",
                    "providers": [
                        {"provider": "primary", "state": "cooldown"},
                        {"provider": "backup", "state": "ready"},
                    ],
                },
            }

    result = await QuickCommandRouter(health=Health(), vps=object()).handle("/health")

    assert "LLM：✅ backup（可用 1/2" in str(result)
