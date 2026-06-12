# Skill Runtime Architecture Design

## Objective

Upgrade Luck-Agent from intent-specific routing and controller registration to
a deterministic Skill architecture that compensates for a weak planning model.

The first release migrates:

- `BlogSkill`
- `LegacyReactSkill`
- `SkillRegistry`
- `SkillRouter`
- SQLite `runtime_events`

PKB, direct commands, file handling, authorization, and Lark protocol parsing
remain outside this migration.

## Design Principles

1. The model generates content inside a bounded workflow; it does not choose
   arbitrary execution architecture.
2. One Skill identity owns matching, Goal construction, execution policy, and
   result metadata.
3. Routing and execution decisions are deterministic and auditable.
4. Every important Runtime transition is appended to SQLite.
5. Existing ReAct behavior remains an explicit fallback during migration.
6. The first release uses Python registration. External `SKILL.md` or YAML
   loading is deferred.

## Boundaries

### Control Plane

The following stay in `agent.py` or existing handlers:

- Lark event decoding
- user authorization
- group mention filtering
- file/image/audio dispatch
- direct command handling
- PKB `#` note shortcut
- model prefix parsing

These operations decide whether a message is eligible for AI processing; they
are not reusable domain capabilities.

### Skill Plane

Skills own:

- message matching
- Skill metadata and permissions
- Goal intent and success criteria
- deterministic step planning
- step execution
- completion checks
- result artifact conventions

## Core Types

### SkillMetadata

```python
@dataclass(frozen=True)
class SkillMetadata:
    name: str
    version: str
    intent: str
    description: str
    execution_mode: Literal["goal_runtime", "legacy_inline"]
    priority: int = 100
    timeout: int = 120
    max_retry: int = 1
    required_permissions: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
```

`name` is the stable persisted identity. `intent` remains for backward
compatibility, reporting, and existing success criteria.

### SkillContext

```python
@dataclass(frozen=True)
class SkillContext:
    user_id: str
    chat_id: str
    text: str
    message_id: str = ""
    model_override: str = ""
```

### SkillMatch

```python
@dataclass(frozen=True)
class SkillMatch:
    matched: bool
    score: float = 0.0
    reason: str = ""
```

Scores are deterministic values between `0.0` and `1.0`. The router chooses the
highest score, then the lowest numeric priority, then the Skill name. This gives
stable behavior across process restarts.

### Skill Protocol

```python
class Skill(Protocol):
    metadata: SkillMetadata

    def match(self, context: SkillContext) -> SkillMatch: ...
```

All registered capabilities implement this routing contract.

### GoalSkill Protocol

```python
class GoalSkill(Skill, Protocol):

    def build_goal(self, context: SkillContext) -> GoalRequest: ...

    async def build_plan(self, goal: dict) -> list[StepSpec]: ...

    async def execute_step(self, goal: dict, step: StepSpec) -> StepResult: ...

    async def is_goal_complete(self, goal: dict, steps: list[dict]) -> bool: ...
```

Only `goal_runtime` Skills implement `GoalSkill`. A `legacy_inline` Skill only
implements the base `Skill` routing contract; RuntimeManager never sends it to
ExecutionEngine.

### GoalRequest

```python
@dataclass(frozen=True)
class GoalRequest:
    title: str
    intent: str
    success_criteria: tuple[str, ...] = ()
    plan: dict[str, Any] = field(default_factory=dict)
```

RuntimeManager adds these persisted plan fields:

```python
{
    "source_message": "...",
    "skill": "blog_write",
    "skill_version": "1.0.0",
}
```

## SkillRegistry

`SkillRegistry` is the authoritative in-memory catalog.

Responsibilities:

- register Skill instances
- reject duplicate names
- reject empty or malformed metadata
- retrieve by stable name
- list only enabled Skills
- resolve a persisted Goal to its Skill
- fall back from old Goals that only contain `intent`

The registry is immutable after Agent startup. Runtime registration or hot
reload is out of scope.

The Registry replaces:

- `RuntimeIntentRouter.BLOG_KEYWORDS`
- `ExecutionEngine.controllers`
- direct `BlogController` registration in `agent.py`

## SkillRouter

`SkillRouter` evaluates all enabled Skills except the legacy fallback.

Routing:

1. Normalize the context text.
2. Call every candidate Skill's `match`.
3. Discard unmatched results.
4. Sort by score descending, priority ascending, name ascending.
5. Select the first result.
6. If no Goal Skill matches, return `LegacyReactSkill`.

The Router does not call a model. Weak-model compensation comes from
deterministic routing and narrow Skill workflows.

Route results include:

```python
{
    "skill": "blog_write",
    "intent": "blog_write",
    "execution_mode": "goal_runtime",
    "score": 0.95,
    "reason": "blog keyword matched",
}
```

## RuntimeManager

RuntimeManager receives `SkillRegistry` and `SkillRouter`.

For a Goal Skill:

1. route the message
2. ask the Skill to build `GoalRequest`
3. create the persisted Goal
4. persist Skill identity/version in Goal plan
5. submit the existing Goal ID to RuntimeTaskQueue
6. return:

```python
{
    "handled": True,
    "skill": "blog_write",
    "goal_id": "...",
    "intent": "blog_write",
    "status": "accepted",
    "queue_status": "pending",
    "summary": "...",
}
```

For `LegacyReactSkill`:

```python
{
    "handled": False,
    "skill": "legacy_react",
    "goal_id": "",
    "intent": "general",
    "reason": "no goal skill matched",
}
```

`agent.py` then invokes the existing `AgentMessageHandler`. No legacy execution
code moves into RuntimeManager.

## ExecutionEngine

ExecutionEngine receives `SkillRegistry` instead of a controller dictionary.

Resolution:

1. Read `goal.plan.skill`.
2. Resolve the exact Skill in the Registry.
3. For pre-migration Goals, resolve by `goal.intent`.
4. Reject `legacy_inline` Skills.

The selected Skill directly supplies `build_plan`, `execute_step`, and
`is_goal_complete`.

This removes the duplicate concept of a Controller. `BaseController` and
`BlogController` are removed after `BlogSkill` passes compatibility tests.

## Initial Skills

### BlogSkill

Metadata:

```python
SkillMetadata(
    name="blog_write",
    version="1.0.0",
    intent="blog_write",
    description="Plan or generate blog content",
    execution_mode="goal_runtime",
    priority=50,
    timeout=180,
    max_retry=1,
)
```

Matching includes:

- 博客
- blog
- 文章
- 写文章
- 博客选题
- 重构博客
- 发布博客

It preserves the current model-backed `generate_content` step and artifact:

```python
{
    "type": "generated_content",
    "content": "...",
    "model": "...",
    "tokens": 0,
}
```

### LegacyReactSkill

Metadata:

```python
SkillMetadata(
    name="legacy_react",
    version="1.0.0",
    intent="general",
    description="Fallback to the existing ReAct message handler",
    execution_mode="legacy_inline",
    priority=10000,
)
```

It is never enqueued and has no tool permissions of its own. Existing
`core.intent_router` and `AgentMessageHandler` continue to constrain tools.

## Runtime Events

### Schema

```sql
CREATE TABLE IF NOT EXISTS runtime_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    goal_id     TEXT DEFAULT '',
    step_id     TEXT DEFAULT '',
    skill       TEXT DEFAULT '',
    intent      TEXT DEFAULT '',
    event_type  TEXT NOT NULL,
    status      TEXT DEFAULT '',
    user_id     TEXT DEFAULT '',
    chat_id     TEXT DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_events_goal
    ON runtime_events(goal_id, id);
CREATE INDEX IF NOT EXISTS idx_runtime_events_skill
    ON runtime_events(skill, id);
CREATE INDEX IF NOT EXISTS idx_runtime_events_type
    ON runtime_events(event_type, id);
```

Events are append-only. Production code receives a `RuntimeEventRecorder`
dependency; a no-op recorder may be used in isolated unit tests.

### Event Types

Routing:

- `route.matched`
- `route.fallback`
- `route.error`

Goal:

- `goal.created`
- `goal.accepted`
- `goal.started`
- `goal.recovered`
- `goal.cancelled`
- `goal.completed`
- `goal.blocked`
- `goal.failed`

Queue and worker:

- `queue.submitted`
- `worker.pickup`
- `worker.interrupted`
- `queue.completed`
- `queue.cancelled`
- `queue.failed`

Steps and supervision:

- `step.created`
- `step.started`
- `step.completed`
- `step.retry`
- `step.blocked`
- `step.failed`
- `supervisor.decision`

Notification:

- `notification.sent`
- `notification.failed`

Payloads contain structured summaries, not secrets or full environment values.
Model text may be truncated in events; complete output remains in Goal artifacts.

## Event Consistency

Events do not replace authoritative Goal and Step tables.

Ordering rule:

1. Commit authoritative state.
2. Append the corresponding event.
3. Continue execution.

If event append fails:

- log `runtime_event_write_failed`
- do not roll back a completed tool or Goal transition
- continue execution

This avoids making observability a new availability dependency.

## Data Flow

```text
Lark message
  -> control-plane filters
  -> SkillRouter
      -> BlogSkill
          -> RuntimeManager creates Goal
          -> Queue
          -> Worker
          -> ExecutionEngine resolves BlogSkill
          -> Skill plan and step execution
          -> Supervisor
          -> Goal terminal state
          -> Lark final notification
      -> LegacyReactSkill
          -> existing AgentMessageHandler
```

The listed routing, Goal, queue, worker, step, supervisor, and notification
transitions append `runtime_events` records. Pure reads and health checks do
not emit events.

## Error Handling

- Duplicate Skill registration fails Agent startup.
- A persisted Goal referencing a missing Skill is blocked with an explicit
  `missing skill` error and `goal.blocked` event.
- Skill match exceptions are isolated, recorded, and treated as no match.
- Skill execution exceptions remain normalized by ExecutionEngine.
- Event recording failures never alter Goal state.
- Legacy fallback remains available if no Goal Skill matches.

## Migration Sequence

1. Add Skill types, Registry, Router, and tests without changing runtime flow.
2. Add `runtime_events` schema, recorder, and query tests.
3. Implement `BlogSkill` by moving BlogController behavior.
4. Implement `LegacyReactSkill`.
5. Switch RuntimeManager from RuntimeIntentRouter to SkillRouter.
6. Switch ExecutionEngine from controller registry to SkillRegistry.
7. Wire event recording at Runtime boundaries.
8. Remove RuntimeIntentRouter and BlogController after compatibility tests pass.
9. Keep `core.intent_router` inside Legacy ReAct until later migration.

## Testing

Tests must prove:

- Registry rejects duplicates and resolves persisted Skills.
- Router tie-breaking is deterministic.
- Blog phrases route to `BlogSkill`.
- General messages return `LegacyReactSkill`.
- RuntimeManager returns the required `skill` field.
- Legacy fallback does not create a Goal or queue item.
- ExecutionEngine resumes a Goal using persisted Skill identity.
- Pre-migration Goals resolve by intent.
- Missing Skills block rather than crash the Worker.
- Runtime events are append-only and ordered per Goal.
- Route, queue, step, supervisor, terminal, and notification events are present.
- Restart recovery preserves Skill identity.
- Existing Runtime and legacy ReAct tests remain green.

## Non-Goals

- dynamic Skill loading
- external Skill marketplace
- model-selected Skills
- PKB migration
- direct command migration
- scheduler migration
- distributed queues
- event replay as the source of truth
