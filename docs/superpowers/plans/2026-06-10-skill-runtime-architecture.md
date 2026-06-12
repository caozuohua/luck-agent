# Skill Runtime Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Runtime-specific intent routing and controller registration with deterministic Skills, while recording important Runtime transitions in append-only SQLite events.

**Architecture:** Introduce focused `skills/` modules for contracts, registry, router, and initial Skills. RuntimeManager creates Goals from `GoalSkill` requests and persists Skill identity; ExecutionEngine resolves execution through the same registry. A best-effort `RuntimeEventRecorder` records observability after authoritative state changes without becoming an availability dependency.

**Tech Stack:** Python 3.10+, asyncio, dataclasses, Protocol, SQLite, unittest

---

## File Map

- Create `skills/base.py`: immutable Skill metadata, context, match, Goal request, and protocols.
- Create `skills/registry.py`: startup-time registration, validation, lookup, and persisted Goal resolution.
- Create `skills/router.py`: deterministic Skill matching and legacy fallback.
- Create `skills/blog.py`: blog matching, Goal construction, plan, execution, and completion.
- Create `skills/legacy_react.py`: explicit non-queued fallback capability.
- Create `skills/__init__.py`: public Skill package exports.
- Create `runtime/events.py`: best-effort Runtime event recorder and no-op recorder.
- Modify `core/memory.py`: add `runtime_events` schema and append/query APIs.
- Modify `runtime/runtime_manager.py`: route through Skills, persist Skill identity, and record routing/Goal/queue events.
- Modify `core/execution_engine.py`: resolve Goal Skills through `SkillRegistry` and record step/supervisor/terminal events.
- Modify `runtime/worker.py`: record worker, queue terminal, and notification events.
- Modify `agent.py`: register Skills once and inject Registry, Router, and event recorder.
- Delete `runtime/intent_router.py`, `controllers/base.py`, and `controllers/blog_controller.py` after compatibility migration.
- Modify and add tests under `tests/` for each boundary.

### Task 1: Skill Contracts, Registry, And Deterministic Router

**Files:**
- Create: `skills/__init__.py`
- Create: `skills/base.py`
- Create: `skills/registry.py`
- Create: `skills/router.py`
- Create: `skills/legacy_react.py`
- Test: `tests/test_skill_registry.py`
- Test: `tests/test_skill_router.py`

- [ ] **Step 1: Write failing registry and router tests**

Cover these exact behaviors:

```python
def test_registry_rejects_duplicate_names():
    registry = SkillRegistry()
    registry.register(FakeSkill(name="alpha"))
    with self.assertRaisesRegex(SkillRegistrationError, "duplicate skill"):
        registry.register(FakeSkill(name="alpha"))

def test_registry_resolves_persisted_skill_before_intent():
    registry = SkillRegistry([FakeGoalSkill(name="new", intent="shared")])
    goal = {"intent": "old", "plan": {"skill": "new"}}
    self.assertEqual(registry.resolve_goal(goal).metadata.name, "new")

def test_router_uses_score_then_priority_then_name():
    registry = SkillRegistry([
        FakeGoalSkill(name="zeta", score=0.8, priority=20),
        FakeGoalSkill(name="alpha", score=0.8, priority=10),
        LegacyReactSkill(),
    ])
    result = SkillRouter(registry).route(SkillContext("u", "c", "request"))
    self.assertEqual(result.skill.metadata.name, "alpha")

def test_router_returns_explicit_legacy_fallback():
    registry = SkillRegistry([FakeGoalSkill(name="none", score=0.0), LegacyReactSkill()])
    result = SkillRouter(registry).route(SkillContext("u", "c", "general question"))
    self.assertEqual(result.skill.metadata.name, "legacy_react")
    self.assertEqual(result.skill.metadata.execution_mode, "legacy_inline")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_skill_registry tests.test_skill_router -v
```

Expected: import failures because `skills` contracts and registry do not exist.

- [ ] **Step 3: Implement contracts, registry, fallback, and router**

Implement:

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

@dataclass(frozen=True)
class SkillContext:
    user_id: str
    chat_id: str
    text: str
    message_id: str = ""
    model_override: str = ""

@dataclass(frozen=True)
class SkillMatch:
    matched: bool
    score: float = 0.0
    reason: str = ""

@dataclass(frozen=True)
class GoalRequest:
    title: str
    intent: str
    success_criteria: tuple[str, ...] = ()
    plan: dict[str, Any] = field(default_factory=dict)
```

`SkillRegistry.register()` must reject malformed metadata and duplicate names. `resolve_goal()` must prefer `goal["plan"]["skill"]`, then fall back to a unique Goal Skill with matching `intent`, and raise `SkillNotFoundError` otherwise. `SkillRouter.route()` must isolate match exceptions and order matches by `(-score, priority, name)`.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_skill_registry tests.test_skill_router -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add skills tests/test_skill_registry.py tests/test_skill_router.py
git commit -m "feat: add skill registry and router"
```

### Task 2: BlogSkill Compatibility Migration

**Files:**
- Create: `skills/blog.py`
- Modify: `tests/test_blog_controller.py`
- Test: `tests/test_blog_skill.py`

- [ ] **Step 1: Write failing BlogSkill tests**

Test exact metadata, all requested phrases, Goal request identity, current generation artifact, unsupported actions, and completion:

```python
def test_blog_phrases_match():
    skill = BlogSkill(generator=FakeGenerator())
    for text in ("博客", "blog", "文章", "写文章", "博客选题", "重构博客", "发布博客"):
        self.assertTrue(skill.match(SkillContext("u", "c", text)).matched)

def test_goal_request_preserves_source_message():
    request = BlogSkill(generator=FakeGenerator()).build_goal(
        SkillContext("u", "c", "帮我整理一个博客选题")
    )
    self.assertEqual(request.intent, "blog_write")
    self.assertEqual(request.plan["source_message"], "帮我整理一个博客选题")
```

Port the existing `BlogControllerTests` execution and completion assertions to `BlogSkill`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_blog_skill -v
```

Expected: import failure because `skills.blog.BlogSkill` does not exist.

- [ ] **Step 3: Implement BlogSkill**

Move the current `BlogController` plan, model generation, artifact, and completion behavior into a `GoalSkill` with metadata:

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

Keep `controllers/content_generator.py` unchanged. Use deterministic normalized substring matching and a score of `0.95`.

- [ ] **Step 4: Run compatibility tests**

Run:

```powershell
python -m unittest tests.test_blog_skill tests.test_blog_controller -v
```

Expected: BlogSkill tests pass and existing generator tests remain green.

- [ ] **Step 5: Commit**

```powershell
git add skills/blog.py tests/test_blog_skill.py tests/test_blog_controller.py
git commit -m "feat: migrate blog workflow to skill"
```

### Task 3: SQLite Runtime Event Recorder

**Files:**
- Modify: `core/memory.py`
- Create: `runtime/events.py`
- Test: `tests/test_runtime_events.py`

- [ ] **Step 1: Write failing persistence and failure-isolation tests**

```python
def test_record_and_list_events_in_goal_order():
    memory = Memory(db_path)
    recorder = RuntimeEventRecorder(memory)
    recorder.record("goal.created", goal_id="g1", skill="blog_write", payload={"n": 1})
    recorder.record("queue.submitted", goal_id="g1", skill="blog_write", payload={"n": 2})
    events = memory.list_runtime_events(goal_id="g1")
    self.assertEqual([event["event_type"] for event in events], ["goal.created", "queue.submitted"])
    self.assertEqual(events[0]["payload"], {"n": 1})

def test_recorder_logs_and_continues_when_sqlite_write_fails():
    recorder = RuntimeEventRecorder(FailingMemory())
    with patch("runtime.events.log") as log:
        recorder.record("goal.created", goal_id="g1")
    log.error.assert_called_once()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_events -v
```

Expected: missing recorder/schema APIs.

- [ ] **Step 3: Add schema and recorder**

Add the exact `runtime_events` schema and indexes from the design specification. Implement:

```python
def append_runtime_event(self, event: dict[str, Any]) -> None: ...
def list_runtime_events(
    self,
    *,
    goal_id: str | None = None,
    skill: str | None = None,
    event_type: str | None = None,
    limit: int = 200,
) -> list[dict]: ...
```

`RuntimeEventRecorder.record()` generates `event_id`, JSON-serializes only through `Memory`, truncates oversized string payload values, catches all persistence errors, logs `runtime_event_write_failed`, and returns without raising. Add `NoopRuntimeEventRecorder` with the same method.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_runtime_events -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add core/memory.py runtime/events.py tests/test_runtime_events.py
git commit -m "feat: persist runtime events"
```

### Task 4: RuntimeManager Skill Routing And Goal Identity

**Files:**
- Modify: `runtime/runtime_manager.py`
- Modify: `tests/test_runtime_integration.py`
- Test: `tests/test_runtime_skill_routing.py`

- [ ] **Step 1: Write failing RuntimeManager Skill tests**

Prove:

```python
async def test_blog_skill_creates_and_queues_goal():
    result = await manager.handle_message(user_id="u", chat_id="c", text="博客选题")
    self.assertEqual(result["skill"], "blog_write")
    self.assertEqual(result["intent"], "blog_write")
    self.assertTrue(result["handled"])
    self.assertEqual(saved_goal["plan"]["skill"], "blog_write")
    self.assertEqual(saved_goal["plan"]["skill_version"], "1.0.0")

async def test_legacy_skill_does_not_create_goal():
    result = await manager.handle_message(user_id="u", chat_id="c", text="hello")
    self.assertFalse(result["handled"])
    self.assertEqual(result["skill"], "legacy_react")
    self.assertEqual(goal_manager.created, [])
```

Also assert `route.matched`/`route.fallback`, `goal.created`, `goal.accepted`, and `queue.submitted` event order.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_skill_routing -v
```

Expected: RuntimeManager lacks Skill dependencies and `skill` result.

- [ ] **Step 3: Migrate RuntimeManager**

Inject `SkillRegistry`, `SkillRouter`, and recorder. Build `SkillContext`, route it, return the explicit legacy result without queueing, and for Goal Skills:

1. call `build_goal()`
2. merge `source_message`, `skill`, and `skill_version` into the persisted plan
3. create Goal with Skill criteria
4. submit using Skill priority
5. return required result contract

Recovery must resolve each persisted Goal through Registry, use Skill priority, preserve the stored Skill in queue metadata, and block missing Skills with `goal.blocked` rather than queueing them.

- [ ] **Step 4: Run RuntimeManager tests**

Run:

```powershell
python -m unittest tests.test_runtime_skill_routing tests.test_runtime_integration -v
```

Expected: all RuntimeManager and recovery tests pass.

- [ ] **Step 5: Commit**

```powershell
git add runtime/runtime_manager.py tests/test_runtime_skill_routing.py tests/test_runtime_integration.py
git commit -m "feat: route runtime messages through skills"
```

### Task 5: ExecutionEngine Skill Dispatch And Lifecycle Events

**Files:**
- Modify: `core/execution_engine.py`
- Modify: `core/goal.py`
- Test: `tests/test_execution_engine_skills.py`
- Modify: `tests/test_runtime_integration.py`

- [ ] **Step 1: Write failing dispatch and event tests**

Cover:

```python
async def test_engine_resolves_persisted_skill_identity():
    goal_id = create_goal(plan={"skill": "blog_write", "skill_version": "1.0.0"})
    goal = await engine.run_goal(goal_id)
    self.assertEqual(goal["status"], "done")

async def test_engine_falls_back_to_intent_for_old_goal():
    goal_id = create_goal(intent="blog_write", plan={"source_message": "old"})
    self.assertEqual((await engine.run_goal(goal_id))["status"], "done")

async def test_missing_skill_blocks_goal():
    goal_id = create_goal(plan={"skill": "removed_skill"})
    goal = await engine.run_goal(goal_id)
    self.assertEqual(goal["status"], "blocked")
    self.assertIn("missing skill", goal["error"])
```

Assert ordered events include `goal.started`, `step.created`, `step.started`, `supervisor.decision`, `step.completed`, and `goal.completed`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_execution_engine_skills -v
```

Expected: engine only supports controller dictionaries.

- [ ] **Step 3: Replace controller dispatch with SkillRegistry**

Remove `GoalController`, `controllers`, `register_controller()`, and `get_controller()`. Inject Registry and recorder, resolve the Goal Skill from the persisted Goal in both `run_goal()` and `run_step()`, and rename internal controller variables/helpers to Skill terminology.

Catch `SkillNotFoundError` in `run_goal()`, persist `blocked` state, and record `goal.blocked`. Record events only after each authoritative Goal/Step mutation. Record a single `supervisor.decision` for each review.

- [ ] **Step 4: Run engine and integration tests**

Run:

```powershell
python -m unittest tests.test_execution_engine_skills tests.test_runtime_integration tests.test_runtime_worker -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add core/execution_engine.py core/goal.py tests/test_execution_engine_skills.py tests/test_runtime_integration.py
git commit -m "feat: execute goals through skill registry"
```

### Task 6: Worker Events, Agent Wiring, And Legacy Removal

**Files:**
- Modify: `runtime/worker.py`
- Modify: `agent.py`
- Modify: `tests/test_runtime_worker.py`
- Modify: `tests/test_runtime_notifications.py`
- Modify: `tests/test_runtime_integration.py`
- Delete: `runtime/intent_router.py`
- Delete: `controllers/base.py`
- Delete: `controllers/blog_controller.py`

- [ ] **Step 1: Write failing worker and wiring tests**

Assert:

- `worker.pickup` follows `queue.submitted`
- terminal queue event matches `queue.completed`, `queue.failed`, or `queue.cancelled`
- successful/failed final notification records `notification.sent`/`notification.failed`
- `AgentApp._init_components` registers `BlogSkill` and `LegacyReactSkill`
- RuntimeManager and ExecutionEngine receive the same Registry and recorder
- no import or registration of `BlogController` or `RuntimeIntentRouter` remains

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_worker tests.test_runtime_notifications tests.test_runtime_integration -v
```

Expected: missing worker events and old wiring assertions fail.

- [ ] **Step 3: Wire recorder and Skills**

Inject the recorder into WorkerManager/RuntimeWorker. Record `worker.pickup` before execution, terminal queue events after successful queue state transitions, `worker.interrupted` after persisted interruption, and `notification.sent` only after the callback returns. Record `notification.failed` from the callback exception before preserving the existing log-and-continue behavior.

In `AgentApp._init_components`:

```python
registry = SkillRegistry()
registry.register(BlogSkill(generator=generator))
registry.register(LegacyReactSkill())
skill_router = SkillRouter(registry)
event_recorder = RuntimeEventRecorder(self._memory)
```

Pass the same objects to RuntimeManager, ExecutionEngine, and WorkerManager. Delete obsolete Runtime intent router and controller modules after all imports and tests migrate.

- [ ] **Step 4: Run full verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q agent.py core controllers runtime skills handlers tools
git diff --check
rg -n "RuntimeIntentRouter|BlogController|register_controller|controllers\\[" agent.py core runtime skills tests
```

Expected: all tests pass, compilation and diff checks are clean, and the final search has no production references to obsolete routing/controller APIs.

- [ ] **Step 5: Commit**

```powershell
git add agent.py runtime/worker.py tests runtime skills core controllers
git commit -m "feat: wire skill runtime lifecycle"
```

### Task 7: End-To-End Completion Audit

**Files:**
- Modify only files required by failures found during the audit.

- [ ] **Step 1: Exercise the authoritative Runtime flow**

Run the integration test that submits `帮我整理一个博客选题` and assert:

```text
SkillRouter -> blog_write
goal.created -> goal.accepted
queue.submitted -> worker.pickup
goal.started -> step.created -> step.started
supervisor.decision -> step.completed
goal.completed -> queue.completed
notification.sent
```

The accepted result must contain:

```python
{
    "handled": True,
    "skill": "blog_write",
    "goal_id": "...",
    "intent": "blog_write",
    "summary": "...",
}
```

- [ ] **Step 2: Verify fallback and restart paths**

Run tests proving a general message returns `legacy_react` without a Goal, and a persisted pre-migration `blog_write` Goal resumes by intent.

- [ ] **Step 3: Run clean full-suite verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q agent.py core controllers runtime skills handlers tools
git status --short
git log --oneline -n 10
```

Expected: full suite passes, compile succeeds, and only intentional files are present.

- [ ] **Step 4: Request final code review**

Review against `docs/superpowers/specs/2026-06-10-skill-runtime-architecture-design.md`, emphasizing deterministic routing, persisted Skill identity, old Goal compatibility, event ordering, and failure isolation.
