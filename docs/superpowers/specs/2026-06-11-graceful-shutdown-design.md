# Graceful Shutdown Design

## Problem

`AgentApp.run()` starts `lark.ws.Client.start()` in asyncio's default executor.
The Lark SDK owns a separate global event loop and waits forever in `_select()`.
On SIGTERM, Luck Agent stops its own workers, queue, scheduler, and health
monitor, but it never stops the SDK loop. `asyncio.run()` then waits for the
default executor thread, so systemd reaches its 90-second stop timeout and
sends SIGKILL.

## Design

Add a focused `LarkWebSocketRunner` adapter under `core/`. It owns the SDK
client's blocking thread lifecycle:

- `start()` schedules the blocking SDK client exactly once.
- `stop()` schedules shutdown work on the SDK event loop.
- Shutdown closes the active WebSocket and cancels SDK background tasks,
  including the permanent `_select()` task.
- The runner waits for the blocking thread to finish with a bounded timeout.
- Repeated `stop()` calls are safe.

`AgentApp.run()` will use this adapter and execute application component
shutdown through a bounded helper. A stuck component is logged and cannot keep
the process alive until systemd sends SIGKILL.

## Goal Recovery

Runtime workers retain their existing cancellation behavior: an in-flight Goal
is persisted as interrupted/recoverable before process exit. On startup,
`recover_runtime_goals()` requeues recoverable Goals before workers begin
processing. The deployment acceptance test must interrupt an active Goal,
restart the service, and prove that the same Goal reaches a terminal state
without duplicate completion notifications.

## Verification

- Unit test the runner start/stop lifecycle with a fake SDK loop and client.
- Unit test bounded application shutdown when a component stalls.
- Run the full suite locally and on the VPS.
- Confirm `systemctl restart luck-agent` returns before the configured stop
  timeout and logs `agent_stopped` without SIGKILL.
- Execute the in-flight Goal restart/recovery scenario and inspect SQLite
  events for one coherent lifecycle.
