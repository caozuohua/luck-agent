from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    vertex_project: str = ""
    vertex_location: str = "us-central1"
    vertex_model: str = "gemini-2.0-flash"
    service_account_key_path: str = "/home/agent/app/sa-key.json"
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
        vertex_project=os.environ.get("VERTEX_PROJECT") or os.environ.get("GCP_PROJECT", ""),
        vertex_location=os.environ.get("VERTEX_LOCATION") or os.environ.get("GCP_LOCATION", "us-central1"),
        vertex_model=os.environ.get("VERTEX_MODEL", "gemini-2.0-flash"),
        service_account_key_path=os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "/home/agent/app/sa-key.json",
        ),
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
