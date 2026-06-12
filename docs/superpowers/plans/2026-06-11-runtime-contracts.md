# Runtime Contracts and Error Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Standardize Runtime message results and Skill failure semantics while removing intent-specific policy from GoalManager.

**Architecture:** Introduce a small immutable mapping-compatible result object at the Runtime boundary and typed Skill execution errors at the Skill boundary. ExecutionEngine converts recoverable errors into StepResults for Supervisor and persists fatal errors directly, while GoalManager retains only generic lifecycle defaults.

**Tech Stack:** Python 3.10+, asyncio, dataclasses, collections.abc Mapping, SQLite, unittest.

---

### Task 1: Typed Runtime Handle Result

**Files:**
- Create: `runtime/contracts.py`
- Modify: `runtime/runtime_manager.py`
- Modify: `agent.py`
- Create: `tests/test_runtime_contracts.py`
- Modify: `tests/test_runtime_skill_routing.py`

- [ ] **Step 1: Write failing value-object tests**

Create tests proving:

```python
accepted = RuntimeHandleResult(
    handled=True,
    skill="blog_write",
    goal_id="goal-1",
    intent="blog_write",
    status="accepted",
    queue_status="pending",
    summary="pending",
    reason="matched",
)
self.assertEqual(accepted.skill, "blog_write")
self.assertEqual(accepted["goal_id"], "goal-1")
self.assertEqual(dict(accepted), accepted.to_dict())
```

Also assert invalid accepted and fallback combinations raise `ValueError`.

- [ ] **Step 2: Run the contract tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_runtime_contracts -v
```

Expected: import failure because `runtime.contracts` does not exist.

- [ ] **Step 3: Implement the immutable mapping contract**

Implement `RuntimeHandleResult` as a frozen dataclass implementing
`collections.abc.Mapping[str, Any]`. Validate:

```python
if self.handled and not all((self.skill, self.goal_id, self.intent)):
    raise ValueError("handled result requires skill, goal_id, and intent")
if not self.handled and self.goal_id:
    raise ValueError("fallback result cannot include goal_id")
```

Expose `to_dict()`, `__getitem__`, `__iter__`, and `__len__`.

- [ ] **Step 4: Migrate RuntimeManager and agent**

Return `RuntimeHandleResult` from both branches of
`RuntimeManager.handle_message()`. Use:

```python
status="fallback"
queue_status=""
summary=""
```

for legacy fallback. Change `agent.py` to use `runtime_result.handled`,
`.goal_id`, and `.summary`.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
python -m unittest tests.test_runtime_contracts tests.test_runtime_skill_routing tests.test_runtime_integration -v
```

Expected: all pass while existing mapping assertions remain valid.

- [ ] **Step 6: Commit**

```powershell
git add runtime/contracts.py runtime/runtime_manager.py agent.py tests/test_runtime_contracts.py tests/test_runtime_skill_routing.py
git commit -m "runtime: type message handling result"
```

### Task 2: Typed Skill Execution Errors

**Files:**
- Modify: `skills/base.py`
- Modify: `core/execution_engine.py`
- Modify: `tests/test_execution_engine_skills.py`
- Modify: `tests/test_skill_registry.py`

- [ ] **Step 1: Write failing error contract tests**

Add tests proving:

```python
RetryableSkillError("provider busy", hint="retry later")
BlockingSkillError("permission required", hint="grant permission")
FatalSkillError("invalid skill state")
```

carry the expected `retryable`, `blocking`, and `hint` values and require a
non-empty safe message.

- [ ] **Step 2: Write failing execution tests**

Use a configurable fake Goal Skill and prove:

- retryable error on attempt one then success produces Goal `done` and
  `retry_count == 1`;
- repeated retryable errors produce Goal `blocked`;
- blocking error produces Goal `blocked` with `retry_count == 0`;
- fatal error produces Step and Goal `failed`;
- unknown `ValueError` produces Step and Goal `failed` with persisted text
  `skill execute failed: ValueError`;
- timeout retries once before blocking;
- fatal execution records `step.failed` and no `supervisor.decision`.

- [ ] **Step 3: Run the new tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_execution_engine_skills -v
```

Expected: missing error classes and existing unknown-error behavior blocks
instead of failing.

- [ ] **Step 4: Implement Skill errors**

In `skills/base.py`, define:

```python
class SkillExecutionError(RuntimeError):
    retryable = False
    blocking = False

    def __init__(self, message: str, *, hint: str = "") -> None:
        message = " ".join(message.split())
        if not message:
            raise ValueError("skill execution error message is required")
        super().__init__(message)
        self.hint = hint

class RetryableSkillError(SkillExecutionError):
    retryable = True

class BlockingSkillError(SkillExecutionError):
    blocking = True

class FatalSkillError(SkillExecutionError):
    pass
```

- [ ] **Step 5: Normalize recoverable and fatal outcomes**

Add a private fatal outcome dataclass in `core/execution_engine.py`.

`_execute_skill_step()` must:

- return non-blocking `StepResult` for timeout and
  `RetryableSkillError`;
- return blocking `StepResult` for `BlockingSkillError`;
- return fatal outcome for `FatalSkillError` and unknown exceptions;
- include only safe message for typed errors and only exception type for
  unknown errors.

`run_step()` must persist a fatal outcome as Step `failed`, emit
`step.failed` with `error_type` and `error_class="fatal"`, and return a fail
decision without calling Supervisor.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
python -m unittest tests.test_execution_engine_skills tests.test_skill_registry tests.test_skill_runtime_e2e -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```powershell
git add skills/base.py core/execution_engine.py tests/test_execution_engine_skills.py tests/test_skill_registry.py
git commit -m "runtime: classify skill execution errors"
```

### Task 3: Move Success Criteria Ownership to Skills

**Files:**
- Modify: `core/goal.py`
- Create: `tests/test_goal_defaults.py`
- Verify: `skills/blog.py`
- Verify: `tests/test_blog_skill.py`

- [ ] **Step 1: Write failing generic-default tests**

Add tests proving omitted criteria are identical for `blog_write`,
`github_code`, `shell_run`, and an unknown intent:

```python
self.assertEqual(
    GoalManager.default_success_criteria("blog_write"),
    GoalManager.default_success_criteria("unknown"),
)
```

Also prove explicit criteria supplied by a Skill are persisted unchanged.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_goal_defaults tests.test_blog_skill -v
```

Expected: generic-default assertion fails because GoalManager contains
intent-specific mappings.

- [ ] **Step 3: Remove intent-specific defaults**

Replace the mapping with one immutable generic sequence:

```python
GENERIC_SUCCESS_CRITERIA = (
    "任务目标已明确",
    "必要步骤已记录",
    "完成、失败或阻塞状态已明确",
)
```

Keep `default_success_criteria(intent)` for compatibility, ignore `intent`,
and return a new list each call. Do not modify `BLOG_SUCCESS_CRITERIA`.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
python -m unittest tests.test_goal_defaults tests.test_blog_skill tests.test_runtime_skill_routing -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add core/goal.py tests/test_goal_defaults.py
git commit -m "core: keep goal defaults domain neutral"
```

### Task 4: Full Verification and Deployment

**Files:**
- Verify all changed files.
- No new source files expected.

- [ ] **Step 1: Run static diff checks**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors and only intended changes.

- [ ] **Step 2: Run the complete local suite**

```powershell
python -m unittest discover -s tests -v
```

Expected: zero failures.

- [ ] **Step 3: Push the branch**

```powershell
git push origin codex/runtime-final-results
```

- [ ] **Step 4: Fast-forward and test on VPS**

Run as the service user:

```bash
cd /opt/luck-agent
sudo -u luck-agent git pull --ff-only origin codex/runtime-final-results
sudo -u luck-agent ./venv/bin/python -m unittest discover -s tests
```

Do not alter `/opt/luck-agent/backup/`.

- [ ] **Step 5: Restart and verify service lifecycle**

```bash
sudo systemctl restart luck-agent
sudo systemctl is-active luck-agent
sudo -u luck-agent git -C /opt/luck-agent rev-parse --short HEAD
```

Verify logs contain worker startup and WebSocket connection without printing
the full WebSocket URL.

- [ ] **Step 6: Real Lark acceptance**

Send `帮我整理一个博客选题` and verify:

- acceptance message is sent before the final result;
- returned runtime contract identifies `skill=blog_write`;
- SQLite events retain:
  `route.matched`, `goal.created`, `goal.accepted`, `queue.submitted`,
  `worker.pickup`, `goal.started`, `step.created`, `step.started`,
  `supervisor.decision`, `step.completed`, `goal.completed`,
  `queue.completed`, `notification.sent`;
- final Goal status is `done`;
- no duplicate final notification is sent.
