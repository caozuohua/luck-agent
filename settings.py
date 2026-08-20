from __future__ import annotations

import os
from dataclasses import dataclass

# Load .env (if present) into os.environ BEFORE reading settings, so the
# file-based config works the same way V1's config.py did. Stdlib-only.
def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                # strip surrounding quotes
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                os.environ.setdefault(key, val)
    except FileNotFoundError:
        pass

_load_dotenv()


@dataclass(frozen=True)
class AgentSettings:
    # LLM — OpenAI-compatible /chat/completions endpoint.
    # Default target: the `new-api` container on the GCP VPS (same host as V2).
    #   base url : http://127.0.0.1:3000/v1   (new-api binds localhost:3000)
    #   auth     : Authorization: <new-api user token>  (from new-api web UI)
    #   model    : whatever the new-api channel serves (see LLM_MODEL)
    # When `llm_base_url` is empty the runtime falls back to an offline
    # FakeLLMClient so the stack + test suite run with no model backend.
    llm_base_url: str = ""  # http://127.0.0.1:3000/v1  (new-api on VPS)
    llm_api_key: str = ""     # new-api user token (from web UI, NOT ROOT_TOKEN env)
    llm_model: str = ""         # e.g. the model name the new-api channel serves
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_failure_threshold: int = 3
    llm_cooldown_seconds: float = 30.0

    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_domain: str = "https://open.larksuite.com"
    lark_allowed_user_ids: str = ""
    lark_allowed_chat_ids: str = ""
    lark_allow_unconfigured: bool = False
    lark_approval_ttl_seconds: float = 300.0
    ops_allowed_targets: str = ""
    ops_allowed_services: str = ""
    ops_allowed_operations: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    serper_api_key: str = ""
    db_path: str = "/home/agent/data/agent.db"
    agent_workdir: str = "/home/agent/workspace"
    shell_timeout_seconds: int = 15
    shell_max_output_chars: int = 4000
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    vps_name: str = ""
    vps_provider: str = "aws"
    vps_account: str = ""
    vps_region: str = ""
    vps_target_id: str = ""
    vps_role: str = "personal"
    vps_sysops_root: str = "/opt/vps_sysops"
    vps_sysops_profile: str = "aws"
    vps_sysops_timeout_seconds: float = 15.0
    mem0_base_url: str = ""
    mem0_api_key: str = ""
    mem0_user_id: str = "personal"
    mem0_agent_id: str = "luck-agent"
    mem0_timeout_seconds: float = 10.0
    curator_trigger_interval: int = 50
    curator_periodic_interval_seconds: float = 24 * 60 * 60
    shutdown_timeout_seconds: float = 30.0
    # LangGraph ReAct engine — production resource controls.
    execution_mode: str = "graph"  # "graph" (LangGraph ReAct) | "legacy"
    max_steps: int = 12  # hard cap on ReAct loop iterations per goal
    max_retry: int = 2  # per-step retry budget (Supervisor)
    graph_db_path: str = "/home/agent/data/graph_state.db"  # checkpointer
    graph_max_active: int = 1  # concurrent graphs per user (task queue)


def load_settings() -> AgentSettings:
    return AgentSettings(
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", "nvidia/llama-3.1-nemotron-nano-8b-v1"),
        llm_timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
        llm_max_retries=int(os.environ.get("LLM_MAX_RETRIES", "2")),
        llm_failure_threshold=int(os.environ.get("LLM_FAILURE_THRESHOLD", "3")),
        llm_cooldown_seconds=float(os.environ.get("LLM_COOLDOWN_SECONDS", "30")),
        lark_app_id=os.environ.get("LARK_APP_ID", ""),
        lark_app_secret=os.environ.get("LARK_APP_SECRET", ""),
        lark_domain=os.environ.get(
            "LARK_DOMAIN",
            "https://open.larksuite.com",
        ),
        lark_allowed_user_ids=os.environ.get("LARK_ALLOWED_USER_IDS", ""),
        lark_allowed_chat_ids=os.environ.get("LARK_ALLOWED_CHAT_IDS", ""),
        lark_allow_unconfigured=os.environ.get("LARK_ALLOW_UNCONFIGURED", "false").lower()
        in {"1", "true", "yes", "on"},
        lark_approval_ttl_seconds=float(os.environ.get("LARK_APPROVAL_TTL_SECONDS", "300")),
        ops_allowed_targets=os.environ.get("OPS_ALLOWED_TARGETS", ""),
        ops_allowed_services=os.environ.get("OPS_ALLOWED_SERVICES", ""),
        ops_allowed_operations=os.environ.get("OPS_ALLOWED_OPERATIONS", ""),
        web_host=os.environ.get("WEB_HOST", "127.0.0.1"),
        web_port=int(os.environ.get("WEB_PORT", "8000")),
        serper_api_key=os.environ.get("SERPER_API_KEY", ""),
        db_path=os.environ.get("DB_PATH", "/home/agent/data/agent.db"),
        agent_workdir=os.environ.get("AGENT_WORKDIR", "/home/agent/workspace"),
        shell_timeout_seconds=int(os.environ.get("SHELL_TIMEOUT_SECONDS", "15")),
        shell_max_output_chars=int(os.environ.get("SHELL_MAX_OUTPUT_CHARS", "4000")),
        health_host=os.environ.get("HEALTH_HOST", "0.0.0.0"),
        health_port=int(os.environ.get("HEALTH_PORT", "8080")),
        vps_name=os.environ.get("VPS_NAME", ""),
        vps_provider=os.environ.get("VPS_PROVIDER", "aws"),
        vps_account=os.environ.get("VPS_ACCOUNT", ""),
        vps_region=os.environ.get("VPS_REGION", ""),
        vps_target_id=os.environ.get("VPS_TARGET_ID", ""),
        vps_role=os.environ.get("VPS_ROLE", "personal"),
        vps_sysops_root=os.environ.get("VPS_SYSOPS_ROOT", "/opt/vps_sysops"),
        vps_sysops_profile=os.environ.get("VPS_SYSOPS_PROFILE", "aws"),
        vps_sysops_timeout_seconds=float(
            os.environ.get("VPS_SYSOPS_TIMEOUT_SECONDS", "15")
        ),
        mem0_base_url=os.environ.get("MEM0_BASE_URL", ""),
        mem0_api_key=os.environ.get("MEM0_API_KEY", ""),
        mem0_user_id=os.environ.get("MEM0_USER_ID", "personal"),
        mem0_agent_id=os.environ.get("MEM0_AGENT_ID", "luck-agent"),
        mem0_timeout_seconds=float(os.environ.get("MEM0_TIMEOUT_SECONDS", "10")),
        curator_trigger_interval=int(os.environ.get("CURATOR_TRIGGER_INTERVAL", "50")),
        curator_periodic_interval_seconds=float(
            os.environ.get("CURATOR_PERIODIC_INTERVAL_SECONDS", str(24 * 60 * 60))
        ),
        shutdown_timeout_seconds=float(os.environ.get("SHUTDOWN_TIMEOUT_SECONDS", "30")),
        execution_mode=os.environ.get("EXECUTION_MODE", "graph"),
        max_steps=int(os.environ.get("MAX_STEPS", "12")),
        max_retry=int(os.environ.get("MAX_RETRY", "2")),
        graph_db_path=os.environ.get("GRAPH_DB_PATH", "/home/agent/data/graph_state.db"),
        graph_max_active=int(os.environ.get("GRAPH_MAX_ACTIVE", "1")),
    )
