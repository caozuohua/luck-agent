# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Luck Agent is a Python-based Lark (飞书) bot that runs on GCP VPS (e2-micro). It connects via WebSocket to Lark, routes messages to Gemini AI models (pro/flash/lite), and provides tools for GitHub operations, shell execution, file management, and web search.

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
- **model_router.py** — Multi-model routing with fallback chain (pro → flash → lite). Uses `google-genai` library with Vertex AI backend. Builds system prompts with user profile, success patterns, and recent context.
- **memory.py** — SQLite persistence (WAL mode) for conversation history, user profiles, task records, and success patterns. Thread-safe via `threading.local()`.
- **task_queue.py** — Async priority queue with configurable workers, retry with exponential backoff, timeout handling, and Lark card notifications on completion.
- **scheduler.py** — Cron-like scheduled tasks stored in SQLite, integrated with message handler.
- **health.py** — System health monitoring: error log retention in SQLite, periodic VACUUM, resource monitoring, WS heartbeat tracking.
- **log.py** — Structured JSON logging (GCP Cloud Logging compatible), replaces structlog with zero dependencies.

### Tools (`tools/`)
- **github_tools.py** — GitHub REST API v3 client with connection pooling (httpx), 429/5xx retry, rate limit handling.
- **shell_tools.py** — Async shell execution with dangerous command blacklist, timeout, output truncation.
- **file_bridge.py** — Lark ↔ VPS file transfer via Lark File API.
- **search_tools.py** — Multi-backend web search (DuckDuckGo, SearXNG, Qwant) with failover.

### Message Cards (`cards/`)
**builder.py** — Lark Card 2.0 JSON builder for interactive cards (task status, GitHub actions, shell output, file lists).

## Configuration

All config via `.env` file (loaded by `config.py` at startup). Required keys:
- `GCP_PROJECT`, `LARK_APP_ID`, `LARK_APP_SECRET`, `GITHUB_TOKEN`

Optional: `GCP_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`, `GITHUB_OWNER`, `LARK_DOMAIN`, `HUGO_REPO`, `DB_PATH`, `SHELL_WORK_DIR`, `FILE_DIR`

## Key Patterns

**Model Selection**: `Config.pick_model(text)` auto-selects based on keywords (分析/写作/规划 → pro) and length (>500 chars → flash, else → lite). Users can force with `/pro`, `/flash`, `/lite` prefix.

**Tool Calling Loop**: ReAct style in `AgentMessageHandler.handle()` — model generates tool calls → execute → inject results → model continues. Max 6 rounds to prevent loops.

**Memory Injection**: System prompt includes user profile, success patterns (learned from past tool calls), and recent conversation context. Tools `remember`/`recall`/`forget` manage user preferences.

**Lark Message Splitting**: `LarkSender` automatically chunks long text (3800 chars) and cards (3500 chars markdown) to stay within Lark API limits.

## Development Notes

- Python 3.10+ required (uses `from __future__ import annotations`, `X | Y` union types)
- All tool functions are async — `ModelRouter` wraps sync `google-genai` calls via `run_in_executor`
- SQLite connections are thread-local (`threading.local()`), not shared across async tasks
- No test suite exists — verify changes by running `python agent.py` and testing via Lark
- GCP auth priority: `.env` key file → GCE ADC → `gcloud auth application-default login`
