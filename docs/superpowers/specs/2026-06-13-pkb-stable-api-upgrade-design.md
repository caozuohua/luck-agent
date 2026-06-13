# PKB Stable API Upgrade Design

## Goal

Upgrade luck-agent from the legacy PKB URL-specific integration to the stable
PKB API while preserving the existing `#` note shortcut and `/pkb` search
command. Expose the full PKB lifecycle to the model through narrowly scoped
tools and keep normal responses available when PKB is unavailable.

## Configuration

Use one base URL and one credential:

```text
PKB_BASE_URL=https://your-pkb.vercel.app
PKB_API_SECRET=...
PKB_TIMEOUT_MS=10000
```

`PKB_BASE_URL` is normalized by removing a trailing slash. All PKB requests
except health checks send `x-api-secret` and `Content-Type: application/json`.
The timeout is interpreted as milliseconds and defaults to 10000.

The old `VERCEL_API_URL`, `PKB_INGEST_URL`, `PKB_SEARCH_URL`, and
`PKB_HEALTH_URL` settings are removed from documented configuration. Runtime
code uses only the new settings so configuration failures are explicit rather
than silently selecting an obsolete endpoint.

## Architecture

Add `tools/pkb_tools.py` as the single PKB HTTP boundary. It owns URL
construction, headers, timeout handling, JSON parsing, retry policy, and
status-specific errors. `handlers/message.py` remains responsible for model
tool declarations, dispatch, and response presentation, but delegates all PKB
operations to the new client.

Existing entry points remain:

- `# ...` parses the note and calls `pkb_save`.
- `/pkb <query>` calls `pkb_search`.
- Existing PKB cards continue to render save and search outcomes.

The client exposes:

- `pkb_save`: `POST /api/pkb`
- `pkb_search`: `POST /api/pkb/search`
- `pkb_get`: `GET /api/pkb/{id}`
- `pkb_list`: `GET /api/pkb/list`
- `pkb_update`: `PATCH /api/pkb/{id}`
- `pkb_delete`: `DELETE /api/pkb/{id}`
- `pkb_restore`: `POST /api/pkb/{id}/restore`
- `check_pkb_health`: unauthenticated `GET /api/pkb/health`

List query parameters are encoded by the HTTP client. `topics` is sent as a
comma-separated value. Optional parameters are omitted rather than sent with
empty values.

## Tool Contracts

Tool names match the stable API names. The legacy model tool names
`write_pkb` and `search_pkb` are replaced by `pkb_save` and `pkb_search`;
direct Python compatibility wrappers may remain only where existing command
handlers benefit from them.

Allowed note types are exactly:

```text
fact | idea | task | question | code
```

`pkb_save` defaults `source` to `luck-agent` and `type` to `fact`.
`pkb_search` defaults to `limit=5` and `action=search`; it omits `source`
unless the user explicitly requests a source restriction. List limits are
clamped to the API range of 1 through 100.

Update requires at least one of `content`, `type`, `topics`, or `summary`.
Delete never sends `hard=true`. Permanent deletion is outside this upgrade;
adding it later requires an explicit user request and a separate confirmation
flow.

## Agent Policy

Prompt and intent-routing guidance instruct the model to:

- save only explicit long-term knowledge, stable preferences, project
  decisions and reasons, reusable conclusions, and long-running tasks;
- never save greetings, transient state, context-free fragments, credentials,
  private keys, tokens, or unconfirmed sensitive personal information;
- search proactively before answering questions about user history, project
  decisions, or long-term preferences;
- use `pkb_get` when search output lacks sufficient context;
- locate a note before update or deletion;
- require user confirmation before soft deletion;
- use `pkb_restore` for undoing a deletion.

The deterministic intent router gains PKB list, update, delete, and restore
coverage where phrases are unambiguous. Ambiguous references such as "delete
that note" are handled by the model with search and confirmation rather than
direct deletion.

## Responses And Idempotency

Successful saves inspect `idempotent`:

- `false`: report that the knowledge was added.
- `true`: report that the knowledge base already contained it, not that a new
  record was created.

Search, get, and list preserve stable result fields including `id`, `title`,
`content`, `type`, `topics`, `created_at`, `deleted_at`, and `url`. Cards and
plain-text fallbacks use the same normalized representation.

PKB failures are returned as structured tool errors. The assistant may
continue answering from current conversation context, but must not claim that
it read or wrote PKB when the operation failed.

## Error Handling

The HTTP boundary applies these rules:

- `400`: return a parameter error; the model may correct and retry once.
- `401`: return a configuration/authentication error without retry.
- `404`: report that the note does not exist or is soft-deleted.
- `409`: report duplicate content; save guidance asks the model to search for
  the existing record.
- `500` and `503`: retry up to two times with exponential backoff.
- timeout/network failure: return an unavailable error after bounded retries
  where appropriate.
- invalid JSON: return a protocol error without inventing a successful result.

Retries are implemented in the client, except the semantic correction retry
for `400`, which remains a model decision because only the caller knows how to
fix the arguments.

## Testing

Add focused unit tests before implementation for:

- base URL normalization, headers, timeout conversion, and unauthenticated
  health checks;
- every HTTP method, route, JSON body, and list query parameter;
- type validation, list limit bounds, and non-empty update validation;
- idempotent save responses;
- 400, 401, 404, 409, invalid JSON, timeout, and 500/503 retry behavior;
- model tool declarations and dispatch for all seven tools;
- intent and prompt policy for proactive search, source omission, protected
  data, confirmation before delete, and soft-delete-only behavior;
- compatibility of `#`, `/pkb`, existing PKB cards, and system health output.

Run the full repository test suite after focused tests pass.

## VPS Verification

After local verification:

1. Update the VPS `.env` with `PKB_BASE_URL`, `PKB_API_SECRET`, and
   `PKB_TIMEOUT_MS=10000` without printing the secret.
2. Deploy the code and restart the `luck-agent` systemd service.
3. Check service status and recent logs for startup or configuration errors.
4. Call the unauthenticated health endpoint.
5. Perform a reversible live flow using non-sensitive test content: save,
   repeat save to verify idempotency, search, get, update, list, soft delete,
   confirm 404/deleted behavior, and restore.
6. Clean up by soft-deleting the test note unless retaining it is needed to
   verify restore; never use hard deletion.

Any live API incompatibility is treated as a failed verification and corrected
in code and tests before completion is reported.
