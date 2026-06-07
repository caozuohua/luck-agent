# Goal Runtime Final Result Design

## Objective

Extend Goal Runtime so a migrated request receives exactly two Lark messages:

1. An immediate task-accepted response.
2. One terminal response containing the final result or failure details.

The initial acceptance case is `帮我整理一个博客选题`, but the design must
support future blog writing, rewriting, and publishing workflows without
changing RuntimeManager or Worker lifecycle behavior.

## Scope

This change covers:

- Model-backed execution inside `BlogController`.
- Persistence of generated step output and final artifacts.
- A terminal Worker callback for `done`, `blocked`, and `failed` goals.
- Lark rendering of the final result.
- Automated tests for successful and failed terminal notifications.

This change does not add per-step Lark notifications, queue persistence, or
additional runtime intents.

## Architecture

### Model Adapter

`BlogController` receives an injected async content generator. The production
adapter uses the existing `ModelRouter`; tests use a deterministic fake.

The controller passes the original source message, goal metadata, and relevant
prior step outputs to the generator. The adapter returns normalized text rather
than exposing provider-specific response structures to the controller.

### Controller Execution

The controller keeps a deterministic persisted plan. For the initial topic
request, one model-backed generation step produces the user-facing result.
Supporting steps may validate context or package artifacts, but they must not
return fabricated success data.

Generated content is stored in the step output. The final user-facing content
is also appended to the Goal artifacts using a stable artifact type such as
`generated_content`.

### Terminal Notification

`WorkerManager` accepts an optional async terminal callback. A worker calls it
once after execution reaches a terminal state:

- `done`: include Goal ID, final status, and generated content.
- `blocked`: include Goal ID, blocking reason, and current step.
- `failed`: include Goal ID and error details.

Queue bookkeeping completes before notification. Notification failures are
logged but do not change the already completed Goal or queue status.

`AgentApp` provides the production callback and sends the Lark message to the
Goal's persisted `chat_id`. Intermediate steps produce no Lark messages.

## Data Flow

1. Lark message enters `AgentApp._on_message`.
2. `RuntimeManager` routes it to `blog_write`, creates the Goal, and submits it.
3. Lark receives the immediate accepted response.
4. Worker picks up the Goal and invokes `ExecutionEngine`.
5. `BlogController` calls the injected model adapter.
6. ExecutionEngine persists step output and Goal artifacts.
7. Goal reaches `done`, `blocked`, or `failed`.
8. Worker invokes the terminal callback once.
9. AgentApp sends the final Lark result.

## Error Handling

- Model exceptions become failed or blocked `StepResult` values through the
  existing ExecutionEngine and Supervisor path.
- Missing generated content is treated as a controller failure, not success.
- Terminal notification exceptions are logged with Goal ID and status.
- Notification retries are out of scope; Goal state remains authoritative and
  can be inspected later.

## Testing

Tests will prove:

- A fake model receives the original request and its output is persisted.
- A successful Goal emits one terminal callback with generated content.
- A failed Goal emits one terminal callback with error details.
- RuntimeManager still returns immediately after queue submission.
- No callback is emitted for individual steps.
- Existing unit tests and Python compilation continue to pass.

## Acceptance Criteria

For `帮我整理一个博客选题`:

- Logs show runtime routing, Goal creation, queue acceptance, worker pickup,
  model-backed step execution, Supervisor decision, and Goal completion.
- Lark first displays task acceptance.
- Lark later displays one final result containing model-generated blog topics.
- No intermediate progress messages are sent.
