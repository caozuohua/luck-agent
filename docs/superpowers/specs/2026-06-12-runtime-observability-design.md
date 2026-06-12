# Runtime Observability and Log Redaction Design

## Goal

Make Goal Runtime diagnosable from Lark and SQLite without exposing
credentials, private request data, or unbounded logs.

This phase adds:

- one reusable redaction boundary for application logs, persisted error logs,
  Runtime event payloads, and journal output;
- safe Lark WebSocket lifecycle logs;
- an administrator-only `/runtime` command for Runtime health and Goal event
  timelines.

It does not add a metrics server, tracing collector, dashboard process,
external database, polling worker, or additional model calls.

## Security Boundary

All text leaving a component through a log, event payload, error-log row, or
operator command response is considered observable output and must pass through
the same redaction rules.

Secrets to redact include:

- URL query parameters named `access_key`, `ticket`, `token`,
  `access_token`, `refresh_token`, `secret`, `client_secret`, `app_secret`,
  `api_key`, `key`, `signature`, or `authorization`;
- HTTP `Authorization`, `Proxy-Authorization`, `Cookie`, and `Set-Cookie`
  header values;
- bearer/basic authorization values;
- JSON or Python mapping fields whose normalized key matches a sensitive key;
- configured application secrets when they are available to the caller.

Redacted values become `[REDACTED]`. Structural context such as URL host, path,
non-sensitive query keys, error class, event type, Goal ID, Skill name, intent,
status, counts, and durations remains visible.

Redaction must be:

- deterministic and idempotent;
- recursive for mappings and sequences;
- cycle-safe and depth/node bounded;
- non-throwing for arbitrary objects;
- applied before truncation so a secret cannot survive in a preview;
- independent of specific Lark credentials.

User and chat IDs are operational identifiers rather than credentials. Existing
structured events may retain them in SQLite, but general logs continue to use
the current shortened form where applicable. `/runtime` must not display full
user or chat IDs.

## Redaction API

Create `core/redaction.py` with:

```python
redact_text(value: object, *, secrets: Iterable[str] = ()) -> str
redact_value(value: object, *, secrets: Iterable[str] = ()) -> Any
```

`redact_text()` handles URLs, headers, authorization values, JSON-like text,
and literal configured secrets.

`redact_value()` recursively preserves ordinary scalar/container shape while
replacing sensitive field values. Unknown objects are converted to redacted
text.

The API owns sensitive-key definitions. Callers must not maintain separate
partial regular-expression lists.

## Logging Integration

`core.log._JsonFormatter` redacts:

- `record.getMessage()`;
- every structured `extra` value;
- formatted exception text.

This protects both application logs and third-party records propagated through
the root logger.

`DBLogHandler` uses the same redactor before writing event, detail, user, and
source fields to SQLite. It must not depend on the JSON formatter running
first, because logging handlers receive the original `LogRecord`.

Lark WebSocket construction changes SDK log level from `INFO` to `WARNING`.
The application emits its own safe lifecycle events:

- `lark_websocket_started`;
- `lark_websocket_stopped`;
- `lark_websocket_failed` with only `error_type`.

The SDK logger may still emit warnings and errors; root redaction remains the
defense in depth boundary.

## Runtime Event Integration

`RuntimeEventRecorder` redacts payload values before applying existing depth,
node, string, and byte bounds.

Top-level event fields remain constrained strings. The recorder redacts these
fields as a defense against malformed Skill metadata or status text, while
preserving normal Goal IDs and event names.

Existing event schema and indexes do not change.

## `/runtime` Command

`/runtime` is a direct command and does not invoke a model.

It is restricted to users allowed by the existing administrator-user
configuration. If no administrators are configured, it follows the
repository's existing admin-command policy rather than inventing a second
authorization mechanism.

### `/runtime`

Returns a concise Runtime overview:

- worker count;
- each worker ID, running state, current Goal ID, processed/failed counters;
- queue counts by state and active capacity;
- recoverable Goal count;
- recent Runtime Goals grouped by status;
- latest Runtime event ID/timestamp.

The response has fixed item limits and contains no event payloads, full user
IDs, full chat IDs, paths, environment values, or provider details.

### `/runtime <goal_id>`

Returns:

- Goal ID, Skill, intent, status, title, current step, progress, and sanitized
  error;
- at most the latest 30 Runtime events in chronological order;
- each event line includes timestamp, event type, status, Step ID, and a
  compact sanitized payload summary;
- payload summaries are length bounded and omit user/chat IDs.

Unknown Goal IDs return a clear not-found response. Invalid identifiers do not
become SQL fragments; the existing parameterized Memory API is used.

## Component Wiring

`CommandHandler` receives references to:

- `RuntimeManager` for queue snapshot;
- `WorkerManager` for health;
- `GoalManager` for recoverable Goal count and Goal summaries.

These references are assigned during `AgentApp` component initialization,
matching existing scheduler/health dependency injection.

To keep ownership clear, Runtime overview assembly lives in
`runtime/observability.py`, not in the command parser. It returns presentation
data or bounded text without performing network operations.

## Failure Handling

- Redaction failures return `[REDACTION_FAILED]` rather than the original
  value.
- Observability query failures produce a sanitized operator error and an
  application log containing only the exception type.
- Failure to read one Runtime source does not fabricate healthy state.
- `/runtime` is read-only and never starts, retries, cancels, resumes, or
  mutates a Goal.
- Event recording failure remains non-fatal to Goal execution.

## Testing

Tests must prove:

1. Text redaction covers sensitive query parameters, auth headers, bearer/basic
   values, nested JSON-like text, literal configured secrets, mixed case,
   repeated values, and already-redacted input.
2. Recursive value redaction covers dictionaries, lists, tuples, cycles,
   arbitrary objects, and bounds without throwing.
3. JSON logs and DB error logs never contain seeded secret values in message,
   extras, or exceptions.
4. Runtime events redact before truncation and persist no seeded secret.
5. Lark SDK is configured at `WARNING`, while application lifecycle logs remain
   available.
6. `/runtime` is admin-restricted and overview output is bounded.
7. `/runtime <goal_id>` uses parameterized existing APIs, orders at most 30
   events chronologically, sanitizes payload/error text, and handles missing
   Goals.
8. The command performs no model or mutation call.
9. Existing Goal Runtime acceptance, execution, notification, restart recovery,
   and event-order tests remain green.

The complete local and VPS test suites must pass before deployment.

## Rollout

1. Add the redaction module and focused adversarial tests.
2. Integrate redaction into JSON logs, DB logs, and Runtime events.
3. Lower Lark SDK verbosity and add safe lifecycle logging.
4. Add the Runtime observability service and admin command.
5. Verify locally, then deploy and test on the VPS.
6. Restart twice and inspect journald/SQLite for seeded or historical
   credential-shaped values without reproducing credentials in operator output.

Rollback is a Git revert. No database migration or new service is required.
