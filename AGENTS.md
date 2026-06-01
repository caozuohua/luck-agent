# Repository Guidelines

## Project Structure & Module Organization
`agent.py` is the main entrypoint and wires together message routing, tool execution, and WebSocket startup. Core runtime code lives in `core/` for routing, memory, scheduling, logging, and health checks. User-facing handlers are in `handlers/`, split into direct commands, AI message handling, and file uploads. Tool integrations live in `tools/`, and Lark card rendering is in `cards/`. Keep new modules inside these existing boundaries; for example, add API integrations under `tools/` and coordination logic under `core/`.

## Build, Test, and Development Commands
- `python -m venv venv` and `pip install -r requirements.txt`: create a local environment and install dependencies.
- `python agent.py`: run the agent locally.
- `bash deploy.sh [--update]`: deploy or refresh the VPS environment.

There is no dedicated automated test suite in the repository. Validate changes by running the agent and exercising the relevant Lark commands or message flows.

## Coding Style & Naming Conventions
Use Python 3.10+ features already present in the codebase, including `from __future__ import annotations` and `X | Y` unions. Follow standard PEP 8 spacing, 4-space indentation, `snake_case` for functions and modules, and `PascalCase` for classes. Prefer small, single-purpose functions and keep async boundaries clear, especially in `tools/` and `handlers/`.

## Testing Guidelines
No framework is currently configured. When adding behavior, test the shortest path that proves the change: command handlers with the matching `/...` input, tool functions with a real or mocked API call, and persistence changes with SQLite-backed flows. If you add tests, place them in a dedicated `tests/` directory and name them `test_*.py`.

## Commit & Pull Request Guidelines
Recent commits use short, imperative summaries with a narrow scope, often naming the changed file or feature. Keep commit messages equally specific, for example `handlers: fix /git push error handling`. Pull requests should explain what changed, why it changed, and how it was verified. Include screenshots or log excerpts when the change affects Lark cards, command output, or deployment behavior.

## Configuration & Secrets
Configuration is loaded from `.env` in `config.py`. Do not commit secrets or local credentials. Required values include `GCP_PROJECT`, `LARK_APP_ID`, `LARK_APP_SECRET`, and `GITHUB_TOKEN`; optional settings cover model selection, paths, and provider fallbacks.
