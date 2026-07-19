# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Luck Agent is a Python-based Lark (飞书) bot. The repository contains **two
coexisting architectures**:

- **V2 (current, in `main.py`)** — the Goal Runtime: `interface/` (Lark WS +
  health), `llm/` (model client), `core/` (agent loop, routing, tools,
  goals), `tools/`, `skills/`, `memory/`, `runtime/`. The LLM layer is
  **OpenAI-compatible** (`llm/openai_compat.py`, any `/chat/completions`
  endpoint: OpenRouter, ModelRoute, Hermes proxy, Ollama, local). When
  `LLM_BASE_URL` is unset the runtime uses an offline `FakeLLMClient`
  (`llm/fake.py`) so the whole stack — and the test suite — runs with no model
  backend.
- **V1 (deployed, in `agent.py`)** — the original minimal bot. Its model layer
  (`core/model_router.py`) still uses **Google Gemini** via the `google-genai`
  library (Gemini AI Studio API, *not* Vertex AI). V1 is what `deploy.sh`
  ships to the GCP VPS today.

> NOTE: Vertex AI was removed. V2 no longer imports `google.auth` / Vertex.
> V1's Gemini usage is separate and retained for now.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the agent
python agent.py

# Deploy to GCP VPS
bash deploy.sh [--update]
```

## Architecture

### Entry Flow
`agent.py` → `AgentApp` class initializes all components and starts WebSocket connection to Lark. Messages arrive via `_on_message()` and are dispatched to:
- `CommandHandler` (`handlers/command.py`) — direct slash commands (`/sh`, `/git`, `/deploy`, `/schedule`, `/mem`, etc.)
- `AgentMessageHandler` (`handlers/message.py`) — AI-powered ReAct loop with tool calling
- `FileMessageHandler` (`handlers/file_handler.py`) — file/image uploads

### Core Modules (`core/`)
- **model_router.py** — *(V1)* Multi-model routing with fallback chain (pro → flash → lite). Uses `google-genai` (Gemini AI Studio API). **V2 does not use this.**
- **agent.py (V2)** — `MinimalAgent` loop: classify intent → route tools → generate → parse → execute → transition goal state. See `runtime/` for the Goal Runtime that drives it.
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

All config via `.env` file (loaded by `config.py` at startup). Required keys:
- `GCP_PROJECT`, `LARK_APP_ID`, `LARK_APP_SECRET`, `GITHUB_TOKEN`

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

**Model Selection (V1)**: `Config.pick_model(text)` auto-selects based on keywords (分析/写作/规划 → pro) and length (>500 chars → flash, else → lite). Users can force with `/pro`, `/flash`, `/lite` prefix.

**Tool Calling Loop (V1)**: ReAct style in `AgentMessageHandler.handle()` — model generates tool calls → execute → inject results → model continues. Max 6 rounds to prevent loops.

**Goal Runtime (V2)**: messages → `RuntimeManager` → Skill → persistent `Goal` → background `Worker` → `ExecutionEngine`. Goals survive restart (`goal_store.recover`).

**Lark Message Splitting**: `LarkSender` automatically chunks long text (3800 chars) and cards (3500 chars markdown) to stay within Lark API limits.

## Development Notes

- Python 3.10+ required (uses `from __future__ import annotations`, `X | Y` union types)
- All tool functions are async — V1 `ModelRouter` wraps sync `google-genai` calls via `run_in_executor`
- SQLite connections are thread-local (`threading.local()`), not shared across async tasks
- Test suite exists: `pytest tests/` (see Testing above). Verify changes with `pwsh ./scripts/test-local.ps1 -All`
- GCP auth priority (V1): `.env` key file → GCE ADC → `gcloud auth application-default login`
