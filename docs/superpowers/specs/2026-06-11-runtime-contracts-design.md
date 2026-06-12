# Runtime Contracts and Error Strategy Design

## Goal

Generalize the Goal Runtime boundary so adding a new Skill does not require
editing `agent.py`, `GoalManager`, `ExecutionEngine`, or `RuntimeWorker`.

This phase standardizes:

- the result returned by `RuntimeManager.handle_message()`;
- the errors a Goal Skill may raise during execution;
- the mapping from Skill failures to Supervisor decisions and Goal states;
- ownership of Skill-specific success criteria.

It does not add processes, worker concurrency, queues, databases, controllers,
or user-visible intermediate notifications.

## Constraints

- Keep one Runtime worker by default on the current VPS.
- Preserve asynchronous submission: message handling must never await
  `ExecutionEngine.run_goal()`.
- Preserve the existing SQLite event model and authoritative Goal lifecycle.
- Preserve the Lark behavior of sending only task acceptance and final result.
- Preserve legacy ReAct fallback for unmatched messages.
- Existing callers that index the Runtime result like a dictionary must keep
  working during migration.
- Persisted Goals created by earlier versions must remain recoverable.

## Public Runtime Result

Add an immutable `RuntimeHandleResult` value object owned by `runtime`.

Fields:

```python
handled: bool
skill: str
goal_id: str
intent: str
status: str
queue_status: str
summary: str
reason: str
```

`RuntimeManager.handle_message()` returns this type for both Goal Runtime and
legacy fallback routes.

The type exposes `to_dict()` and read-only mapping access so existing
`result["handled"]` and `result["goal_id"]` callers continue to work. New code
should prefer attributes.

Invariants:

- `handled=True` requires non-empty `skill`, `goal_id`, and `intent`.
- `handled=False` requires an empty `goal_id`.
- accepted Goal Runtime work has `status="accepted"`.
- fallback work has `status="fallback"` and an empty `queue_status`.
- `summary` is human-readable presentation data, not an execution input.

`agent.py` consumes attributes after this migration. The compatibility mapping
remains for external callers and tests until a later explicit removal.

## Skill Error Contract

Add explicit Skill execution errors in `skills.base`:

```python
class SkillExecutionError(RuntimeError):
    retryable: bool
    blocking: bool
    hint: str

class RetryableSkillError(SkillExecutionError)
class BlockingSkillError(SkillExecutionError)
class FatalSkillError(SkillExecutionError)
```

The exception message is safe for persisted Goal and Step errors. Private
provider payloads, credentials, request bodies, and stack traces must not be
placed in the message.

Semantics:

| Error | StepResult | Supervisor outcome |
|---|---|---|
| `RetryableSkillError` | `ok=False`, `blocking=False` | retry while budget remains, then block |
| `BlockingSkillError` | `ok=False`, `blocking=True` | block immediately |
| `FatalSkillError` | terminal execution failure | fail Goal immediately |
| timeout | `ok=False`, `blocking=False` | retry while budget remains, then block |
| unknown exception | terminal execution failure with exception type only | fail Goal immediately |

Unknown programming errors must not be presented as recoverable human blocks.
They indicate a broken Skill implementation or Runtime integration and
therefore transition the Goal to `failed`.

## Execution Flow

`ExecutionEngine._execute_skill_step()` returns a normalized internal outcome:

- a valid `StepResult`;
- or a fatal execution marker containing a sanitized reason and error type.

`run_step()` persists all outcomes before returning:

- retryable/blocking results continue through `Supervisor`;
- fatal outcomes mark the Step `failed`, emit `step.failed`, and return a
  `SupervisorDecision(decision="fail")`.

`run_goal()` remains the sole owner of the final Goal transition and emits one
terminal Goal event. `RuntimeWorker` remains responsible for synchronizing the
queue, logging the authoritative terminal state, and sending the final
notification once.

No Skill exception may escape to `RuntimeWorker` during normal execution.
Worker-level exception handling remains as a last-resort containment boundary
for persistence or Runtime defects.

## Skill-Owned Goal Definition

Each Goal Skill owns:

- routing match;
- Goal title;
- intent;
- success criteria;
- initial plan metadata;
- executable steps;
- completion check.

`GoalManager.create_goal()` retains only generic fallback success criteria for
callers that omit them. It must not contain `blog_write`, `github_code`,
`shell_run`, or other intent-specific definitions.

`BlogSkill` continues to provide its current criteria through `GoalRequest`.
Older persisted Goals keep their stored criteria and require no migration.

## Events

The existing event names remain stable.

Additional error payload fields are allowed:

```json
{
  "error_type": "RetryableSkillError",
  "error_class": "retryable"
}
```

Requirements:

- event payloads contain sanitized error type/class, not exception repr;
- `step.failed` is emitted for fatal Skill errors;
- `supervisor.decision` is emitted only when Supervisor actually reviewed a
  `StepResult`;
- exactly one of `goal.completed`, `goal.blocked`, or `goal.failed` is emitted
  for the authoritative terminal transition;
- queue and notification events retain their current ordering.

## Compatibility

- Existing `BlogSkill` successful execution is unchanged.
- Existing unsupported-action behavior remains an immediate block.
- Existing Goal Runtime result dictionary indexing remains supported.
- Existing legacy fallback returns the same logical fields with explicit
  `status="fallback"`.
- Existing persisted Goal resolution by `plan.skill`, with intent fallback for
  old records, remains unchanged.
- The database schema does not change.

## Testing

Tests must prove:

1. `RuntimeHandleResult` validates invariants, supports attributes,
   `to_dict()`, and compatibility indexing.
2. RuntimeManager returns the same result type for accepted and fallback
   routes.
3. Retryable Skill errors retry and can later succeed.
4. Retryable errors exhaust their budget into `blocked`.
5. Blocking Skill errors block without retry.
6. Fatal Skill errors and unknown exceptions fail the Step and Goal.
7. Timeouts use retry budget rather than blocking immediately.
8. Fatal execution emits `step.failed` without a false
   `supervisor.decision`.
9. `GoalManager` contains no intent-specific success criteria.
10. Blog acceptance, execution, final notification, recovery, and event-order
    end-to-end tests remain green.

The full local and VPS suites must pass before deployment.

## Rollout

1. Add and test `RuntimeHandleResult`.
2. Migrate RuntimeManager and `agent.py` to the typed result.
3. Add and test Skill error classes and execution mapping.
4. Remove intent-specific defaults from GoalManager.
5. Run the full test suite locally.
6. Push, run the full suite on the VPS, restart the service, and verify startup
   and one real blog-topic request.

Rollback is a normal Git revert. No data migration or queue drain is required.
