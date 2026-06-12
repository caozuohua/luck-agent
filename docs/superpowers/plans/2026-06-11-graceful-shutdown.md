# Graceful Shutdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Luck Agent exit promptly on SIGTERM and recover an in-flight Runtime Goal after restart.

**Architecture:** Wrap the blocking Lark SDK client in a lifecycle adapter that
can cancel the SDK-owned event loop tasks and join its executor future.
Centralize bounded component shutdown in `AgentApp` and preserve the existing
Runtime recovery path.

**Tech Stack:** Python 3.10+, asyncio, lark-oapi, unittest, systemd, SQLite.

---

### Task 1: Lark WebSocket lifecycle adapter

**Files:**
- Create: `core/lark_ws_runner.py`
- Create: `tests/test_lark_ws_runner.py`

- [ ] Write a failing test proving `stop()` closes the connection, cancels SDK
  tasks, and waits for the blocking future.
- [ ] Run `python -m unittest tests.test_lark_ws_runner -v` and confirm RED.
- [ ] Implement `LarkWebSocketRunner` with idempotent start/stop and timeout.
- [ ] Run the focused test and confirm GREEN.
- [ ] Commit the adapter and test.

### Task 2: Agent shutdown integration

**Files:**
- Modify: `agent.py`
- Create: `tests/test_agent_shutdown.py`

- [ ] Write failing tests proving the runner is stopped and stalled component
  shutdown is bounded.
- [ ] Run `python -m unittest tests.test_agent_shutdown -v` and confirm RED.
- [ ] Integrate the runner and a bounded shutdown helper into `AgentApp.run()`.
- [ ] Run focused tests and the full suite.
- [ ] Commit the integration.

### Task 3: Deploy and recover an in-flight Goal

**Files:**
- No source changes expected.

- [ ] Push the branch and update the VPS with `git pull --ff-only`.
- [ ] Run compilation and all tests as the `luck-agent` service user.
- [ ] Restart the service and confirm shutdown finishes without SIGKILL.
- [ ] Start a deliberately delayed Runtime Goal, restart during execution, and
  verify the same Goal is recovered and completed once.
- [ ] Record the commit, service state, and authoritative SQLite event order.
