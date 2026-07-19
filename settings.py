from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    # LLM — OpenAI-compatible /chat/completions endpoint.
    # Default target: the NIM-served model on the GCP VPS (newAPI).
    # When `llm_base_url` is empty the runtime falls back to an offline
    # FakeLLMClient so the stack + test suite run with no model backend.
    llm_base_url: str = ""  # e.g. http://<vps-ip>:8000/v1  (NIM OpenAI-compatible)
    llm_api_key: str = ""     # NIM bearer / nvapi-... (empty OK for local NIM)
    llm_model: str = "nvidia/llama-3.1-nemotron-nano-8b-v1"  # cold, fast NIM model (override via LLM_MODEL)

    lark_app_id: str = ""
    lark_app_secret: str = ""
    serper_api_key: str = ""
    db_path: str = "/home/agent/data/agent.db"
    agent_workdir: str = "/home/agent/workspace"
    shell_timeout_seconds: int = 15
    shell_max_output_chars: int = 4000
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    curator_trigger_interval: int = 50
    curator_periodic_interval_seconds: float = 24 * 60 * 60
    shutdown_timeout_seconds: float = 30.0


def load_settings() -> AgentSettings:
    return AgentSettings(
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", "nvidia/llama-3.1-nemotron-nano-8b-v1"),
        lark_app_id=os.environ.get("LARK_APP_ID", ""),
        lark_app_secret=os.environ.get("LARK_APP_SECRET", ""),
        serper_api_key=os.environ.get("SERPER_API_KEY", ""),
        db_path=os.environ.get("DB_PATH", "/home/agent/data/agent.db"),
        agent_workdir=os.environ.get("AGENT_WORKDIR", "/home/agent/workspace"),
        shell_timeout_seconds=int(os.environ.get("SHELL_TIMEOUT_SECONDS", "15")),
        shell_max_output_chars=int(os.environ.get("SHELL_MAX_OUTPUT_CHARS", "4000")),
        health_host=os.environ.get("HEALTH_HOST", "0.0.0.0"),
        health_port=int(os.environ.get("HEALTH_PORT", "8080")),
        curator_trigger_interval=int(os.environ.get("CURATOR_TRIGGER_INTERVAL", "50")),
        curator_periodic_interval_seconds=float(
            os.environ.get("CURATOR_PERIODIC_INTERVAL_SECONDS", str(24 * 60 * 60))
        ),
        shutdown_timeout_seconds=float(os.environ.get("SHUTDOWN_TIMEOUT_SECONDS", "30")),
    )
