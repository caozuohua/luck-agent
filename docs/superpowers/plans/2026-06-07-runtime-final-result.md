# Goal Runtime Final Result Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Goal Runtime generate real model-backed blog output and send exactly one terminal Lark result after the immediate acceptance message.

**Architecture:** Add a provider-neutral content generator around the existing `ModelRouter`, inject it into `BlogController`, and persist generated text as a Goal artifact. Add a terminal callback to runtime workers and implement that callback with a focused Lark notifier wired by `AgentApp`.

**Tech Stack:** Python 3.10+, asyncio, unittest, SQLite, existing `ModelRouter`, Lark Card 2.0.

---

## File Structure

- Create `controllers/content_generator.py`: provider-neutral model content generation.
- Modify `controllers/blog_controller.py`: deterministic one-step model-backed blog workflow.
- Modify `runtime/worker.py`: terminal status handling and one-shot callback.
- Create `runtime/notifications.py`: terminal Goal-to-Lark card formatting and sending.
- Modify `agent.py`: dependency injection and production wiring.
- Create `tests/test_blog_controller.py`: controller/model persistence behavior.
- Create `tests/test_runtime_worker.py`: terminal callback and queue behavior.
- Create `tests/test_runtime_notifications.py`: final Lark success/failure rendering.
- Modify `tests/test_runtime_integration.py`: full accepted-to-final-result flow.

### Task 1: Model Content Generator

**Files:**
- Create: `controllers/content_generator.py`
- Create: `tests/test_blog_controller.py`

- [ ] **Step 1: Write the failing generator test**

```python
class FakeRouter:
    def __init__(self) -> None:
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "text": "1. AI Agent 长任务恢复机制",
            "tool_calls": [],
            "model": "fake-model",
            "tokens": 12,
        }


class ModelContentGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_uses_original_goal_message(self) -> None:
        router = FakeRouter()
        generator = ModelContentGenerator(router=router, model_name="fake-model")
        goal = {
            "user_id": "u1",
            "title": "帮我整理一个博客选题",
            "plan": {"source_message": "帮我整理一个博客选题"},
        }

        result = await generator.generate(goal)

        self.assertIn("AI Agent", result.text)
        self.assertEqual(result.model, "fake-model")
        self.assertEqual(
            router.calls[0]["messages"],
            [{"role": "user", "content": "帮我整理一个博客选题"}],
        )
        self.assertEqual(router.calls[0]["tools_schema"], [])
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m unittest tests.test_blog_controller.ModelContentGeneratorTests -v
```

Expected: import failure because `controllers.content_generator` does not exist.

- [ ] **Step 3: Implement the generator**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


BLOG_GENERATION_SYSTEM = """你是博客内容策划助手。
严格根据用户原始请求生成可直接展示的中文结果。
如果用户要求选题，给出 5-10 个具体选题，每项包含标题、切入点和目标读者。
不要声称已写文件、提交代码或发布文章，除非后续工具步骤真实完成。"""


@dataclass(frozen=True)
class GeneratedContent:
    text: str
    model: str = ""
    tokens: int = 0


class ModelContentGenerator:
    def __init__(self, *, router, model_name: str) -> None:
        self.router = router
        self.model_name = model_name

    async def generate(self, goal: dict[str, Any]) -> GeneratedContent:
        source_message = str(
            (goal.get("plan") or {}).get("source_message")
            or goal.get("title")
            or ""
        ).strip()
        if not source_message:
            raise ValueError("goal source message is empty")

        result = await self.router.chat(
            model_name=self.model_name,
            messages=[{"role": "user", "content": source_message}],
            tools_schema=[],
            system=BLOG_GENERATION_SYSTEM,
            user_id=str(goal.get("user_id") or ""),
        )
        text = str(result.get("text") or "").strip()
        if not text:
            raise ValueError("model returned empty content")
        return GeneratedContent(
            text=text,
            model=str(result.get("model") or self.model_name),
            tokens=int(result.get("tokens") or 0),
        )
```

- [ ] **Step 4: Run the generator test and verify GREEN**

Run:

```powershell
python -m unittest tests.test_blog_controller.ModelContentGeneratorTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add controllers/content_generator.py tests/test_blog_controller.py
git commit -m "feat: add runtime content generator"
```

### Task 2: Model-Backed Blog Controller

**Files:**
- Modify: `controllers/blog_controller.py`
- Modify: `tests/test_blog_controller.py`

- [ ] **Step 1: Write failing controller tests**

```python
class FakeGenerator:
    def __init__(self, text: str = "选题结果") -> None:
        self.text = text
        self.goals = []

    async def generate(self, goal):
        self.goals.append(goal)
        return GeneratedContent(text=self.text, model="fake-model", tokens=8)


class BlogControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_contains_one_real_generation_step(self) -> None:
        controller = BlogController(generator=FakeGenerator())

        plan = await controller.build_plan({"goal_id": "g1"})

        self.assertEqual([step.action for step in plan], ["generate_content"])

    async def test_generate_step_returns_persistable_artifact(self) -> None:
        controller = BlogController(generator=FakeGenerator("选题 A\n选题 B"))
        goal = {"goal_id": "g1", "title": "整理博客选题"}
        step = StepSpec(name="generate_content", action="generate_content")

        result = await controller.execute_step(goal, step)

        self.assertTrue(result.ok)
        self.assertEqual(result.data["content"], "选题 A\n选题 B")
        self.assertEqual(
            result.artifacts,
            [{
                "type": "generated_content",
                "content": "选题 A\n选题 B",
                "model": "fake-model",
                "tokens": 8,
            }],
        )
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_blog_controller.BlogControllerTests -v
```

Expected: FAIL because `BlogController` does not accept `generator` and still returns four placeholder steps.

- [ ] **Step 3: Replace placeholder execution**

Implement `BlogController` with:

```python
class BlogController(BaseController):
    intent = "blog_write"

    def __init__(self, *, generator) -> None:
        self.generator = generator

    async def build_plan(self, goal: dict) -> list[StepSpec]:
        return [
            StepSpec(
                name="generate_content",
                action="generate_content",
                timeout=180,
                max_retry=1,
            )
        ]

    async def execute_step(self, goal: dict, step: StepSpec) -> StepResult:
        if step.action != "generate_content":
            return StepResult(
                ok=False,
                action=step.action,
                error=f"unsupported action: {step.action}",
                blocking=True,
            )

        generated = await self.generator.generate(goal)
        artifact = {
            "type": "generated_content",
            "content": generated.text,
            "model": generated.model,
            "tokens": generated.tokens,
        }
        return StepResult(
            ok=True,
            action=step.action,
            data={"content": generated.text},
            artifacts=[artifact],
        )

    async def is_goal_complete(self, goal: dict, steps: list[dict]) -> bool:
        return self.all_required_steps_done(steps)
```

- [ ] **Step 4: Run controller tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_blog_controller -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add controllers/blog_controller.py tests/test_blog_controller.py
git commit -m "feat: generate blog runtime content"
```

### Task 3: Worker Terminal Callback

**Files:**
- Modify: `runtime/worker.py`
- Create: `tests/test_runtime_worker.py`

- [ ] **Step 1: Write failing success and failure callback tests**

```python
class FakeQueue:
    def __init__(self) -> None:
        self.done = []
        self.failed = []

    async def mark_done(self, goal_id):
        self.done.append(goal_id)

    async def mark_failed(self, goal_id, error):
        self.failed.append((goal_id, error))


class FakeEngine:
    def __init__(self, result):
        self.result = result

    async def run_goal(self, goal_id):
        return {**self.result, "goal_id": goal_id}


class RuntimeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_done_goal_marks_queue_done_then_notifies_once(self) -> None:
        events = []
        queue = FakeQueue()

        async def notify(goal):
            events.append(("notify", goal["status"]))
            self.assertEqual(queue.done, ["g1"])

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine({"status": "done", "artifacts": []}),
            terminal_callback=notify,
        )
        await worker._process_item(
            RuntimeQueueItem(goal_id="g1", user_id="u1", chat_id="c1")
        )

        self.assertEqual(events, [("notify", "done")])
        self.assertEqual(queue.failed, [])

    async def test_blocked_goal_marks_queue_failed_and_notifies_once(self) -> None:
        notified = []
        queue = FakeQueue()

        async def notify(goal):
            notified.append(goal)

        worker = RuntimeWorker(
            worker_id="w1",
            queue=queue,
            execution_engine=FakeEngine({
                "status": "blocked",
                "error": "model unavailable",
                "artifacts": [],
            }),
            terminal_callback=notify,
        )
        await worker._process_item(
            RuntimeQueueItem(goal_id="g1", user_id="u1", chat_id="c1")
        )

        self.assertEqual(queue.done, [])
        self.assertEqual(queue.failed, [("g1", "model unavailable")])
        self.assertEqual(len(notified), 1)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_worker -v
```

Expected: FAIL because `terminal_callback` is not accepted and blocked goals are marked done.

- [ ] **Step 3: Implement terminal handling**

Add:

```python
from collections.abc import Awaitable, Callable

TerminalCallback = Callable[[dict[str, Any]], Awaitable[None]]
```

Update constructors to accept and propagate `terminal_callback: TerminalCallback | None = None`.

Replace the success body in `_process_item` with:

```python
try:
    goal = await self.execution_engine.run_goal(item.goal_id)
except Exception as exc:
    error = f"{type(exc).__name__}: {exc}"
    try:
        goal = self.execution_engine.goal_manager.fail_goal(item.goal_id, error)
    except Exception:
        goal = {
            "goal_id": item.goal_id,
            "user_id": item.user_id,
            "chat_id": item.chat_id,
            "status": "failed",
            "error": error,
            "artifacts": [],
        }

status = str(goal.get("status") or "failed")
if status == "done":
    await self.queue.mark_done(item.goal_id)
    self.state.processed += 1
    self.state.last_error = ""
else:
    error = str(goal.get("error") or f"goal ended with status {status}")
    await self.queue.mark_failed(item.goal_id, error)
    self.state.failed += 1
    self.state.last_error = error

if self.terminal_callback:
    try:
        await self.terminal_callback(goal)
    except Exception as notify_error:
        log.error(
            "runtime_terminal_notify_failed",
            goal_id=item.goal_id,
            status=status,
            error=str(notify_error),
        )
```

After the callback block, log `runtime_goal_done` only for `done`; otherwise log
`runtime_goal_failed` with the persisted terminal status and error. Keep
callback exceptions isolated from Goal and queue state.

- [ ] **Step 4: Run worker tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_runtime_worker -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add runtime/worker.py tests/test_runtime_worker.py
git commit -m "feat: notify runtime terminal goals"
```

### Task 4: Lark Terminal Notifier

**Files:**
- Create: `runtime/notifications.py`
- Create: `tests/test_runtime_notifications.py`

- [ ] **Step 1: Write failing notifier tests**

```python
class FakeSender:
    def __init__(self) -> None:
        self.calls = []

    async def send(self, chat_id, text=None, card=None, reply_to=None):
        self.calls.append({
            "chat_id": chat_id,
            "text": text,
            "card": card,
            "reply_to": reply_to,
        })


class RuntimeGoalNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_done_goal_sends_generated_content_card(self) -> None:
        sender = FakeSender()
        notifier = RuntimeGoalNotifier(sender=sender, card_builder=CardBuilder)

        await notifier.notify({
            "goal_id": "g1",
            "chat_id": "c1",
            "status": "done",
            "artifacts": [{
                "type": "generated_content",
                "content": "选题 A\n选题 B",
                "model": "gemini-test",
            }],
        })

        call = sender.calls[0]
        self.assertEqual(call["chat_id"], "c1")
        self.assertIn("选题 A", str(call["card"]))
        self.assertIn("g1", str(call["card"]))

    async def test_failed_goal_sends_error_card(self) -> None:
        sender = FakeSender()
        notifier = RuntimeGoalNotifier(sender=sender, card_builder=CardBuilder)

        await notifier.notify({
            "goal_id": "g2",
            "chat_id": "c2",
            "status": "failed",
            "error": "model unavailable",
            "artifacts": [],
        })

        self.assertIn("model unavailable", str(sender.calls[0]["card"]))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_notifications -v
```

Expected: import failure because `runtime.notifications` does not exist.

- [ ] **Step 3: Implement the notifier**

```python
from __future__ import annotations


class RuntimeGoalNotifier:
    def __init__(self, *, sender, card_builder) -> None:
        self.sender = sender
        self.card_builder = card_builder

    async def notify(self, goal: dict) -> None:
        chat_id = str(goal.get("chat_id") or "")
        if not chat_id:
            raise ValueError("goal chat_id is empty")

        goal_id = str(goal.get("goal_id") or "")
        status = str(goal.get("status") or "failed")
        if status == "done":
            artifact = next(
                (
                    item for item in reversed(goal.get("artifacts") or [])
                    if item.get("type") == "generated_content"
                ),
                None,
            )
            if not artifact or not str(artifact.get("content") or "").strip():
                raise ValueError("completed goal has no generated content")
            card = self.card_builder.agent_reply(
                text=str(artifact["content"]),
                model=str(artifact.get("model") or ""),
                task_id=goal_id,
            )
        else:
            error = str(goal.get("error") or f"goal ended with status {status}")
            card = self.card_builder.error(
                f"任务{status}",
                f"Goal `{goal_id}`\n\n{error}",
            )

        await self.sender.send(chat_id, card=card)
```

- [ ] **Step 4: Run notifier tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_runtime_notifications -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add runtime/notifications.py tests/test_runtime_notifications.py
git commit -m "feat: add runtime Lark notifier"
```

### Task 5: Production Wiring

**Files:**
- Modify: `agent.py`
- Modify: `tests/test_runtime_integration.py`

- [ ] **Step 1: Extend the integration test**

Add this test using a real temporary SQLite database:

```python
class EndToEndGenerator:
    async def generate(self, goal):
        return GeneratedContent(
            text="1. AI Agent 长任务恢复机制",
            model="fake-model",
            tokens=10,
        )


class RuntimeEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepted_goal_produces_one_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            memory = Memory(str(Path(tmp) / "runtime.db"))
            goal_manager = GoalManager(memory)
            queue = RuntimeTaskQueue(max_active=1)
            engine = ExecutionEngine(
                goal_manager=goal_manager,
                supervisor=Supervisor(memory=memory),
            )
            engine.register_controller(
                BlogController(generator=EndToEndGenerator())
            )
            notifications = []

            async def notify(goal):
                notifications.append(goal)

            manager = RuntimeManager(
                goal_manager=goal_manager,
                execution_engine=engine,
                queue=queue,
            )
            workers = WorkerManager(
                queue=queue,
                execution_engine=engine,
                worker_count=1,
                terminal_callback=notify,
            )
            workers.start()
            try:
                accepted = await manager.handle_message(
                    user_id="u1",
                    chat_id="c1",
                    text="帮我整理一个博客选题",
                )
                for _ in range(100):
                    goal = goal_manager.get_goal(accepted["goal_id"])
                    if goal["status"] in {
                        "done", "blocked", "failed", "cancelled"
                    }:
                        break
                    await asyncio.sleep(0.02)

                snapshot = await queue.snapshot()
                self.assertTrue(accepted["handled"])
                self.assertEqual(accepted["status"], "accepted")
                self.assertEqual(len(notifications), 1)
                self.assertEqual(notifications[0]["status"], "done")
                self.assertEqual(
                    notifications[0]["artifacts"][-1]["content"],
                    "1. AI Agent 长任务恢复机制",
                )
                self.assertEqual(snapshot["counts"], {"done": 1})
            finally:
                await workers.stop()
```

Add imports for `asyncio`, `tempfile`, `Path`, `BlogController`,
`GeneratedContent`, `ExecutionEngine`, `GoalManager`, `Memory`, `Supervisor`,
and `WorkerManager`.

- [ ] **Step 2: Run integration test and verify RED**

Run:

```powershell
python -m unittest tests.test_runtime_integration -v
```

Expected: FAIL until `AgentApp`-equivalent dependencies are wired with the
generator and terminal callback.

- [ ] **Step 3: Wire production dependencies**

In `AgentApp._init_components`:

```python
from controllers.content_generator import ModelContentGenerator
from runtime.notifications import RuntimeGoalNotifier

content_generator = ModelContentGenerator(
    router=self._router,
    model_name=cfg.MODEL_PRO,
)
execution_engine.register_controller(
    BlogController(generator=content_generator)
)
runtime_notifier = RuntimeGoalNotifier(
    sender=self._sender,
    card_builder=CardBuilder,
)
self._runtime_workers = WorkerManager(
    queue=runtime_queue,
    execution_engine=execution_engine,
    worker_count=1,
    terminal_callback=runtime_notifier.notify,
)
```

Keep the existing immediate acceptance response unchanged. Do not add
step-level sends.

- [ ] **Step 4: Run integration test and verify GREEN**

Run:

```powershell
python -m unittest tests.test_runtime_integration -v
```

Expected: PASS with one accepted result and one terminal callback.

- [ ] **Step 5: Commit**

```powershell
git add agent.py tests/test_runtime_integration.py
git commit -m "feat: wire runtime final results"
```

### Task 6: Full Verification

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run the complete test suite**

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Compile all Python modules**

```powershell
python -m compileall -q agent.py core controllers runtime handlers tools
```

Expected: exit code 0 and no output.

- [ ] **Step 3: Check patch integrity**

```powershell
git diff --check
rg -n "^(<<<<<<<|=======|>>>>>>>)" agent.py core controllers runtime handlers tools tests
```

Expected: no whitespace errors and no merge-conflict markers.

- [ ] **Step 4: Run the manual VPS acceptance flow**

Send:

```text
帮我整理一个博客选题
```

Expected:

- First Lark message: task accepted with Goal ID and `pending`.
- Logs: `runtime_intent_routed`, `goal_created`, `runtime_goal_accepted`,
  `runtime_worker_pickup`, `model_called`, `goal_step_reviewed`, `goal_done`.
- Second and final Lark message: model-generated blog topics.
- No intermediate step messages.
