# Repository Guidelines

## Project Structure & Module Organization
This repo has **two coexisting architectures**:

- **V2 (current, `main.py`)** — Goal Runtime. `interface/` (Lark WS + health),
  `llm/` (model client), `core/` (agent loop, routing, tools, goals),
  `tools/`, `skills/`, `memory/`, `runtime/`, `controllers/`, `soul/`.
- **V1 (deployed, `agent.py`)** — original minimal bot; `handlers/`, `cards/`,
  `core/model_router.py` (Gemini via `google-genai`, *not* Vertex).

The V2 LLM layer is **OpenAI-compatible** (`llm/openai_compat.py`); when
`LLM_BASE_URL` is unset it uses an offline `FakeLLMClient` (`llm/fake.py`) so
the whole stack and test suite run with no model backend. **Vertex AI was
removed.**

Keep new modules inside existing boundaries: API integrations under `tools/`,
coordination logic under `core/`, domain flows under `controllers/` or `skills/`.

## Build, Test, and Development Commands
- `uv venv --python 3.12` and `uv pip install -r requirements.txt`: create a local environment and install dependencies.
- `python main.py`: run the V2 runtime locally (offline FakeLLMClient unless `LLM_BASE_URL` is set).
- `python agent.py`: run the V1 bot locally.
- `bash deploy.sh [--update]`: deploy or refresh the VPS environment (ships V1).
- **Tests (Windows/Hermes):** the Hermes runtime injects a `PYTHONPATH` that breaks `google-genai`/`lark-oapi` imports. Run with `PYTHONPATH=` cleared, or use the helper:
  - `pwsh ./scripts/test-local.ps1` (unit + integration, offline)
  - `pwsh ./scripts/test-local.ps1 -All` (full suite)

## Coding Style & Naming Conventions
Use Python 3.10+ features already present in the codebase, including `from __future__ import annotations` and `X | Y` unions. Follow standard PEP 8 spacing, 4-space indentation, `snake_case` for functions and modules, and `PascalCase` for classes. Prefer small, single-purpose functions and keep async boundaries clear, especially in `tools/` and `handlers/`.

## Testing Guidelines
A pytest suite exists (`asyncio_mode=auto` in `pytest.ini`):
- `tests/unit` + `tests/integration` — offline, no cloud.
- `tests/` root — full V2 Goal Runtime suite (also offline via FakeLLMClient).
- Name new tests `test_*.py` under `tests/unit` or `tests/integration`.
- V1 handler/command flows are not yet covered by automated tests.

## Commit & Pull Request Guidelines
Recent commits use short, imperative summaries with a narrow scope, often naming the changed file or feature. Keep commit messages equally specific, e.g. `handlers: fix /git push error handling`. Pull requests should explain what changed, why it changed, and how it was verified. Include screenshots or log excerpts when the change affects Lark cards, command output, or deployment behavior.

## Configuration & Secrets
Configuration is loaded from `.env` (V1 via `config.py`; V2 via `settings.py`). Do not commit secrets or local credentials.
V1 required: `GCP_PROJECT`, `LARK_APP_ID`, `LARK_APP_SECRET`, `GITHUB_TOKEN`.
V2 LLM (optional, unset = offline): `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.
