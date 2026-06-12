# Runtime Observability and Log Redaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent credentials from reaching observable outputs and provide a bounded, read-only Runtime diagnostic command.

**Architecture:** A standard-library redaction module becomes the single output-sanitization boundary for logging and Runtime events. A small Runtime observability service composes existing Goal, Queue, Worker, and SQLite data for the direct `/runtime` command without adding background work or storage.

**Tech Stack:** Python 3.10+, logging, urllib.parse, json, re, asyncio, SQLite, unittest.

---

### Task 1: Redaction Core

**Files:**
- Create: `core/redaction.py`
- Create: `tests/test_redaction.py`

- [ ] Write failing tests for sensitive URL query parameters, auth headers,
  bearer/basic values, mixed-case keys, nested JSON strings, literal configured
  secrets, repeated values, idempotence, nested containers, tuples, cycles,
  arbitrary objects, and non-throwing bounded traversal.
- [ ] Run `python -m unittest tests.test_redaction -v` and confirm the import
  failure is the expected RED result.
- [ ] Implement `redact_text()` and `redact_value()` with centralized
  sensitive-key matching, URL parsing, recursive traversal, cycle/depth/node
  bounds, and `[REDACTED]`/`[REDACTION_FAILED]` sentinels.
- [ ] Run the focused tests and `git diff --check`.
- [ ] Commit with `core: add observable data redaction`.

### Task 2: Protect Logs, Events, and Lark Lifecycle

**Files:**
- Modify: `core/log.py`
- Modify: `core/health.py`
- Modify: `runtime/events.py`
- Modify: `core/lark_ws_runner.py`
- Modify: `agent.py`
- Create: `tests/test_log_redaction.py`
- Modify: `tests/test_runtime_events.py`
- Modify: `tests/test_lark_ws_runner.py`
- Modify: `tests/test_runtime_integration.py`

- [ ] Write failing tests proving seeded secrets are absent from JSON log
  messages, structured extras, exception text, DB error-log rows, and Runtime
  event rows.
- [ ] Write a failing test proving redaction occurs before Runtime event payload
  truncation.
- [ ] Write failing tests proving the Lark SDK client receives
  `log_level=lark.LogLevel.WARNING` and the runner emits
  `lark_websocket_started`.
- [ ] Add process-wide `configure_redaction_secrets()` in `core.log`; call it
  after configuration is loaded with non-empty app/provider secrets.
- [ ] Apply redaction independently in `_JsonFormatter`, `DBLogHandler`, and
  `RuntimeEventRecorder`.
- [ ] Lower SDK verbosity to WARNING and add the safe start lifecycle event.
- [ ] Run focused tests:

```powershell
python -m unittest tests.test_redaction tests.test_log_redaction tests.test_runtime_events tests.test_lark_ws_runner tests.test_runtime_integration -v
```

- [ ] Commit with `core: redact observable runtime output`.

### Task 3: Runtime Observability Command

**Files:**
- Create: `runtime/observability.py`
- Modify: `handlers/command.py`
- Modify: `agent.py`
- Create: `tests/test_runtime_observability.py`
- Modify: `tests/test_command_system.py`

- [ ] Write failing service tests for a bounded overview containing worker
  health, queue counts, active capacity, recoverable count, Goal status counts,
  and latest event metadata without user/chat IDs or payloads.
- [ ] Write failing timeline tests proving missing Goal handling, at most 30
  events, chronological ordering, compact redacted payloads, bounded output,
  and no mutation.
- [ ] Implement `RuntimeObservability` as a read-only service over
  `GoalManager`, `WorkerManager`, `RuntimeManager`, and `Memory`.
- [ ] Add `/runtime` help/dispatch. With no argument return overview; with a
  Goal ID return the bounded timeline. Catch failures by exception type only.
- [ ] Inject Runtime dependencies in `AgentApp._init_components()`.
- [ ] Redact `/journal` command output through the same text redactor before
  sending it to Lark.
- [ ] Run focused tests:

```powershell
python -m unittest tests.test_runtime_observability tests.test_command_system tests.test_runtime_integration tests.test_security_hardening -v
```

- [ ] Commit with `runtime: add bounded diagnostics`.

### Task 4: Verification and Deployment

**Files:**
- Verify all changed files.

- [ ] Run `git diff --check` and inspect `git status --short`.
- [ ] Run `python -m unittest discover -s tests -v`; require zero failures.
- [ ] Search source for duplicate sensitive-key regex lists and remove any
  introduced outside `core/redaction.py`.
- [ ] Push `codex/runtime-final-results`.
- [ ] On the VPS, fast-forward as `luck-agent` and run the complete suite.
- [ ] Restart the service twice; verify clean shutdown/startup, one Worker,
  recovery scan, and no new INFO-level full WebSocket URL.
- [ ] Query journald through a boolean/count-only shell check for
  `access_key=`, `ticket=`, seeded test secrets, and authorization values. Do
  not print matching lines.
- [ ] Inspect recent SQLite Runtime events and error-log rows using a
  count-only secret-pattern query.
- [ ] Exercise `/runtime` and `/runtime <goal_id>` through tests or Lark,
  confirming bounded sanitized output and no Goal mutation.
