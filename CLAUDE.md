# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Luck Agent is a Python-based Lark international bot for multi-cloud VPS
operations and Lark platform automation. The repository has one formal
architecture:

- **V2 (in `main.py`)** — the Goal Runtime: `interface/` (Lark WS +
  health), `llm/` (model client), `core/` (agent loop, routing, tools,
  goals), `tools/`, `skills/`, `memory/`, `runtime/`. The LLM layer is
  **OpenAI-compatible** (`llm/openai_compat.py`, any `/chat/completions`
  endpoint: OpenRouter, ModelRoute, Hermes proxy, Ollama, local). When
  `LLM_BASE_URL` is unset the runtime uses an offline `FakeLLMClient`
  (`llm/fake.py`) so the whole stack — and the test suite — runs with no model
  backend.
The V1/Gemini/Vertex implementation is historical and is not part of the
current deployment or development target.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the V2 runtime
python main.py

# Deploy to GCP VPS
bash deploy.sh [--update]
```

## Architecture

### Entry Flow
`main.py` → `Runtime` initializes SQLite, Goal Runtime, health endpoint and the
Lark interface. Real Lark WebSocket acceptance is still pending; local runs
without Lark credentials use the Web interface.

### Core Modules (`core/`)
- **Provider Router** — planned multi-provider fallback, quota detection and circuit breaking for LLM limits. LLM is an enhancement layer, never the VPS control plane.
- **core/agent.py (V2)** — `MinimalAgent` loop: classify intent → route tools → generate → parse → execute → transition goal state. See `runtime/` for the Goal Runtime that drives it.
- **router.py (V2)** — `ToolRouter`: zero-LLM rule-based tool routing from `config/routing_rules.yaml`, with a file-watchdog for hot reload.
- **memory.py (V1)** — SQLite persistence (WAL mode) for conversation history, user profiles, task records, and success patterns. Thread-safe via `threading.local()`.
- **goal.py / execution_engine.py (V2)** — Goal lifecycle + skill execution.
- **health.py** — System health monitoring: error log retention in SQLite, periodic VACUUM, resource monitoring, WS heartbeat tracking.
- **log.py** — Structured JSON logging (GCP Cloud Logging compatible), zero dependencies.

### LLM layer (`llm/`) — V2
- **base.py** — `LLMClient` protocol (`generate`, `repair`).
- **openai_compat.py** — `OpenAICompatClient`: talks to any OpenAI-compatible `/chat/completions` endpoint (OpenRouter, ModelRoute, Hermes proxy, Ollama, local). Selected when `LLM_BASE_URL` is set.
- **fake.py** — `FakeLLMClient`: deterministic offline stand-in used when `LLM_BASE_URL` is **unset** (local dev + the entire test suite). **Vertex AI was removed.**

### Tools (`tools/`)
- **github_tools.py** — GitHub REST API v3 client with connection pooling (httpx), 429/5xx retry, rate limit handling.
- **shell_tools.py** — Async shell execution with dangerous command blacklist, timeout, output truncation.
- **file_bridge.py** — Lark ↔ VPS file transfer via Lark File API.
- **search_tools.py** — Multi-backend web search (DuckDuckGo, SearXNG, Qwant) with failover.
- **pkb_tools.py** — Personal knowledge base client (Vercel + Supabase).

### Message Cards (`cards/`)
**builder.py** — Lark Card 2.0 JSON builder for interactive cards (task status, GitHub actions, shell output, file lists).

## Configuration

All config via `.env` file (loaded by `settings.py` at startup). Lark and cloud
credentials are deployment-specific.

Optional: `GCP_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`, `GITHUB_OWNER`, `LARK_DOMAIN`, `HUGO_REPO`, `DB_PATH`, `SHELL_WORK_DIR`, `FILE_DIR`

### V2 LLM env (used by `main.py`)
- `LLM_BASE_URL` — OpenAI-compatible base URL. **Unset = offline FakeLLMClient.**
- `LLM_API_KEY` — bearer token for that endpoint.
- `LLM_MODEL` — model name (default `gpt-4o-mini`). `VERTEX_*` vars are still read for backwards compatibility.

## Testing

The repo has a real test suite (pytest, `asyncio_mode=auto`):
- `tests/unit` + `tests/integration` — offline, no cloud (V2 FakeLLMClient).
- `tests/` root — full V2 Goal Runtime suite (also offline).
- V1 handler/command flows are not covered by automated tests.

**Windows / Hermes gotcha:** the Hermes runtime injects `PYTHONPATH` pointing at
its own (broken) `pydantic_core`, which breaks `google-genai`/`lark-oapi`
imports. Run tests with `PYTHONPATH=` cleared, or use the helper:

```bash
pwsh ./scripts/test-local.ps1            # unit + integration (fast, offline)
pwsh ./scripts/test-local.ps1 -All       # full suite
```

## Key Patterns

**Goal Runtime (V2)**: messages → `RuntimeManager` → Skill → persistent `Goal` → background `Worker` → `ExecutionEngine`. Goals survive restart (`goal_store.recover`).

**Lark Message Splitting**: `LarkSender` automatically chunks long text (3800 chars) and cards (3500 chars markdown) to stay within Lark API limits.

## Development Notes

- Python 3.10+ required (uses `from __future__ import annotations`, `X | Y` union types)
- All tool functions are async and must preserve explicit user/target boundaries.
- LLM calls are optional; no-LLM VPS operations must remain deterministic and testable.
- Test suite exists: `pytest tests/` (see Testing above). Verify changes with `pwsh ./scripts/test-local.ps1 -All`
- Real Lark WebSocket acceptance and multi-cloud provider integration are pending.
