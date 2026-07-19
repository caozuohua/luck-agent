# LangGraph ReAct Execution Engine — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the single-call `MinimalAgent.run_turn` with a bounded, production-controllable **ReAct graph loop** (Think→Act→Observe→repeat) driven by LangGraph, reusing the *already-present* `core/supervisor.py` (verify/retry/block) and `core/tool_executor.py`, so luck-agent can execute multi-step long tasks instead of one-shot tool calls.

**Architecture:** A LangGraph `StateGraph` with nodes `plan → act → observe → supervise`, looping until the LLM emits `is_goal_complete: true` (DONE), the Supervisor returns `block`/`fail`, or a hard `max_steps` cap is hit. The graph calls the existing `LLMClient`, `ToolExecutor`, and `Supervisor` — none of those are rewritten. A thin `core/graph/engine.py` lazily imports `langgraph` and falls back to a built-in minimal loop if the package is absent, keeping the runtime offline-runnable (tests use `FakeLLMClient`).

**Tech Stack:** Python 3.12, `langgraph>=0.2` (lazy/optional), existing `aiosqlite`/`httpx`, stdlib only otherwise. `llm/base.py` `FakeLLMClient` for offline tests.

---

## Current Context / Findings (verified by reading the repo)

- `core/agent.py::MinimalAgent.run_turn` (lines 89–111) does **ONE** LLM call + **ONE** `tool_executor.execute` and returns. No loop, no Observation feed-back, no ReAct. This is why "the agent does nothing useful."
- `core/supervisor.py` **already exists** with `review_step_result()` returning `pass|retry|block|fail` + lesson capture — but `run_turn` never calls it.
- `runtime/runtime_manager.py` + `runtime/task_queue.py` provide a bounded `RuntimeTaskQueue(max_active=1)` — reusable for the concurrency cap.
- `core/tool_executor.py` already supports `timeout_seconds` (default 30) and command-name→shell aliasing.
- `skills/legacy_react.py` is an empty stub — the old ReAct was removed, never replaced. We rebuild it properly.
- `langgraph` is **NOT installed** (`ModuleNotFoundError`). Plan gates it behind lazy import + offline fallback.
- `interface/web.py` and `interface/lark_ws.py` both call **only** `agent.run_turn(text, *, user_id)` — keep that signature → non-breaking.

**Resource controls (explicit user ask — "控制资源占用和依赖"):**
- `max_steps` per goal (default 12) — hard ReAct iteration cap
- `max_retry` per step (default 2) — enforced via Supervisor
- `max_active=1` — single concurrent goal (reuse `RuntimeTaskQueue`)
- `step_timeout` — per-tool timeout already in `ToolExecutor`
- `context_budget_total` (32k) — already in `MinimalAgent`, preserved
- **Dependency control:** `langgraph` is optional at runtime; offline fallback loop uses only stdlib. Tests never need network (use `FakeLLMClient`).

---

## Proposed Approach

Introduce a `core/graph/` package:
- `state.py` — `GraphState` dataclass (scratchpad: list of turn dicts, step_count, goal, last_tool_result, decision).
- `nodes.py` — the four node functions (plan/act/observe/supervise), each taking `GraphState` → return updated state + next edge.
- `engine.py` — builds the `StateGraph`, wires edges, compiles; `run_graph(state) -> final str`. Lazy-imports `langgraph`; if missing, uses `_run_minimal_loop` (stdlib equivalent with identical node contract).
- `contract.py` — the ReAct LLM output schema (extends current `output_parser`): each step emits `intent` (ACTION/CHAT/DONE/CLARIFY/CANNOT_COMPLETE), `plan`, `tool_call{name,args}`, `is_goal_complete: bool`.

`MinimalAgent.run_turn` becomes: build `GraphState`, call `engine.run_graph`, return final string. All existing helpers (`_build_history_summary`, `_record_turn`, Supervisor via engine) stay.

---

## Step-by-Step Plan

### Task 1: Define the ReAct graph state + contract

**Objective:** Data structures the loop carries between nodes.

**Files:**
- Create: `core/graph/__init__.py`
- Create: `core/graph/state.py`
- Create: `core/graph/contract.py`
- Test: `tests/test_graph_state.py`

**Step 1: Write failing test**
```python
# tests/test_graph_state.py
from core.graph.state import GraphState

def test_state_starts_empty_scratchpad():
    s = GraphState(goal="list files in /tmp")
    assert s.step_count == 0
    assert s.scratchpad == []
    assert s.decision is None

def test_append_observation():
    s = GraphState(goal="x")
    s.append_observation(role="tool", content="file1.txt")
    assert len(s.scratchpad) == 1
    assert s.scratchpad[0]["content"] == "file1.txt"
```

**Step 2: Run to verify failure** — `pytest tests/test_graph_state.py` → FAIL (module missing).

**Step 3: Implement**
```python
# core/graph/__init__.py
from core.graph.state import GraphState
from core.graph.engine import run_graph

# core/graph/state.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass
class GraphState:
    goal: str
    user_id: str = "default"
    step_count: int = 0
    max_steps: int = 12
    scratchpad: list[dict[str, str]] = field(default_factory=list)
    last_tool_result: dict[str, Any] | None = None
    decision: str | None = None  # pass|retry|block|fail|done
    final_answer: str = ""

    def append_observation(self, role: str, content: str) -> None:
        self.scratchpad.append({"role": role, "content": content})

    def budget_exhausted(self) -> bool:
        return self.step_count >= self.max_steps
```

```python
# core/graph/contract.py
from __future__ import annotations
from typing import Any

# ReAct step output schema (superset of existing output_parser intent set).
# LLM must return ONE JSON object:
#   {"intent": "ACTION"|"CHAT"|"DONE"|"CLARIFY"|"CANNOT_COMPLETE",
#    "plan": str, "tool_call": {"name": str, "args": {}},
#    "is_goal_complete": bool, "message": str}
REACT_SYSTEM_HINT = (
    "You operate in a ReAct loop. Each turn return ONE JSON object. "
    "Set is_goal_complete=true only when the user's goal is fully achieved. "
    "After a tool result (Observation), reason about it and decide the next Action."
)
```

**Step 4: Run to verify pass** — `pytest tests/test_graph_state.py` → PASS.

**Step 5: Commit** — `git add core/graph tests/test_graph_state.py && git commit -m "feat(graph): add ReAct state + contract"`

---

### Task 2: Implement the four graph nodes (plan/act/observe/supervise)

**Objective:** Node functions that do the real work, reusable by both LangGraph and the fallback loop.

**Files:**
- Create: `core/graph/nodes.py`
- Test: `tests/test_graph_nodes.py`

**Step 1: Write failing test (uses FakeLLMClient so no network)**
```python
# tests/test_graph_nodes.py
from core.graph.nodes import plan_node, act_node, supervise_node
from core.graph.state import GraphState
from llm.fake import FakeLLMClient
from tools.registry import ToolRegistry
from tools.shell import ShellTool

def test_plan_node_appends_thought():
    reg = ToolRegistry(); reg.register_builtin_tools()
    llm = FakeLLMClient(model="fake")
    s = GraphState(goal="what time is it")
    # FakeLLMClient returns canned ACTION tool_call; just assert no crash + step increments
    out = asyncio.run(plan_node(s, llm=llm, tools=reg.list(), history=""))
    assert out.step_count == 1
```

**Step 2: Run** — FAIL (module missing).

**Step 3: Implement** (key logic, abridged — full code in PR):
```python
# core/graph/nodes.py
from __future__ import annotations
import asyncio, json
from typing import Any
from core.graph.state import GraphState
from core.output_parser import OutputParser, ParseError
from core.tool_executor import ToolExecutor
from core.supervisor import Supervisor

async def plan_node(state, *, llm, tools, history, intent_classifier, router, parser):
    # build prompt (reuse PromptBuilder), call llm.generate, parse
    raw = await llm.generate(system, task)
    parsed = await _safe_parse(parser, raw)
    state.last_parsed = parsed
    state.append_observation("thought", raw)
    state.step_count += 1
    return state

async def act_node(state, *, executor: ToolExecutor):
    parsed = state.last_parsed
    if parsed is None or parsed.tool_call is None:
        state.decision = "done" if parsed and parsed.intent in ("CHAT","DONE") else "fail"
        return state
    result = await executor.execute(parsed.tool_call.name, parsed.tool_call.args, user_id=state.user_id)
    state.last_tool_result = result.to_dict()
    state.append_observation("observation", json.dumps(result.to_dict(), ensure_ascii=False))
    return state

async def supervise_node(state, *, supervisor: Supervisor, goal: dict, max_retry: int):
    if state.last_parsed and state.last_parsed.intent in ("CHAT","DONE") and state.last_parsed.get("is_goal_complete"):
        state.decision = "done"; return state
    if state.last_tool_result is None:
        state.decision = "done"; return state
    dec = supervisor.review_step_result(goal=goal, step={}, result=_wrap(state.last_tool_result), retry_count=state.step_count, max_retry=max_retry)
    state.decision = dec.decision  # pass|retry|block|fail
    return state
```
(`observe_node` merges tool result into the next plan prompt — fold into `plan_node` reading `state.scratchpad`.)

**Step 4: Run** — `pytest tests/test_graph_nodes.py` → PASS.

**Step 5: Commit** — `git commit -m "feat(graph): plan/act/supervise nodes"`

---

### Task 3: Build the LangGraph engine with offline fallback

**Objective:** Wire nodes into a loop; lazy-import langgraph; stdlib fallback.

**Files:**
- Create: `core/graph/engine.py`
- Test: `tests/test_graph_engine.py`

**Step 1: Write failing test** (offline — uses FakeLLMClient, asserts loop terminates within max_steps and returns a string):
```python
def test_engine_runs_to_completion_offline():
    from core.graph.engine import run_graph
    reg = ToolRegistry(); reg.register_builtin_tools()
    llm = FakeLLMClient(model="fake")
    state = GraphState(goal="hi", max_steps=5)
    result = asyncio.run(run_graph(state, llm=llm, tools=reg, supervisor=Supervisor(), history=""))
    assert isinstance(result, str)
```

**Step 2: Run** — FAIL.

**Step 3: Implement:**
```python
# core/graph/engine.py
from __future__ import annotations
import asyncio
from typing import Any
from core.graph.state import GraphState

def _build_graph():
    try:
        from langgraph.graph import StateGraph, END
    except ImportError:
        return None
    # build StateGraph with nodes plan/act/supervise and conditional edges
    # (full code in PR; edges: plan->act->supervise; supervise->plan if pass/retry;
    #  supervise->END if done/block/fail; also END if state.budget_exhausted())
    ...

async def run_graph(state: GraphState, *, llm, tools, supervisor, history="", **kw) -> str:
    builder = _build_graph()
    if builder is None:
        return await _run_minimal_loop(state, llm=llm, tools=tools, supervisor=supervisor, history=history)
    # compile + ainvoke
    ...

async def _run_minimal_loop(state, *, llm, tools, supervisor, history) -> str:
    # stdlib-only equivalent: same plan/act/supervise calls in a while loop
    while not state.budget_exhausted():
        await plan_node(state, llm=llm, tools=tools.list(), history=history, ...)
        await act_node(state, executor=ToolExecutor(tools))
        await supervise_node(state, supervisor=supervisor, goal={}, max_retry=2)
        if state.decision in ("done","block","fail"):
            break
    return state.final_answer or _summarize(state)
```

**Step 4: Run** — `pytest tests/test_graph_engine.py` → PASS (both langgraph-present and fallback paths covered).

**Step 5: Commit** — `git commit -m "feat(graph): LangGraph engine + offline fallback loop"`

---

### Task 4: Wire engine into MinimalAgent.run_turn (non-breaking)

**Objective:** `run_turn` delegates to the graph; web UI & Lark untouched.

**Files:**
- Modify: `core/agent.py:89-111` (`run_turn`)
- Test: `tests/integration/test_agent_flow.py` (already exists, must still pass)

**Step 1: Write failing test** — extend `tests/integration/test_agent_flow.py` with a multi-step assertion (goal requiring 2 tool calls). Run → current code can't loop → demonstrates gap.

**Step 2: Implement `run_turn`:**
```python
async def run_turn(self, user_input, *, user_id="default") -> str:
    self.state = AgentState.IDLE
    goal = await self._create_goal(user_id, user_input)
    history = self._build_history_summary()
    state = GraphState(goal=user_input, user_id=user_id, max_steps=self.max_steps,
                       context_budget_total=self.context_budget_total)
    final = await run_graph(state, llm=self.llm_client, tools=self.tool_registry,
                            supervisor=self._supervisor(), history=history)
    self._record_turn(user_input, final)
    return final
```
Add `self._supervisor()` returning a `Supervisor(memory=self.pattern_store)`.

**Step 3: Run** — `pytest tests/integration/test_agent_flow.py` → PASS.

**Step 4: Commit** — `git commit -m "refactor(agent): run_turn delegates to ReAct graph"`

---

### Task 5: Resource caps + dependency hygiene

**Objective:** Honor "control resource & deps": caps enforced, langgraph optional, requirements updated.

**Files:**
- Modify: `settings.py` (add `max_steps`, `max_retry`, `graph_max_active`)
- Modify: `requirements.txt` (add `langgraph>=0.2` under optional comment)
- Modify: `core/graph/engine.py` (read caps from state, passed by `run_turn`)
- Test: `tests/test_graph_caps.py` (assert loop stops at max_steps; assert block on supervisor fail)

**Step 1: Failing test** — `test_loop_stops_at_max_steps` with `max_steps=2` and a FakeLLM that always returns ACTION → after 2 steps returns (no infinite loop).

**Step 2: Implement caps** — `GraphState.max_steps` already gates; `supervise_node` returns `block` when `retry_count >= max_retry`; `RuntimeTaskQueue(max_active=settings.graph_max_active)` wraps goal submission in `main.py` (reuse existing).

**Step 3: requirements.txt** — add:
```
# ─── Optional graph engine (LangGraph) ───────────────────────
# Runtime falls back to a stdlib loop if absent; tests use FakeLLMClient (no net).
langgraph>=0.2
```

**Step 4: Run** — `pytest tests/test_graph_caps.py tests/test_graph_engine.py` → PASS.

**Step 5: Commit** — `git commit -m "feat(graph): resource caps + optional langgraph dep"`

---

### Task 6: Full suite + web smoke + push

**Objective:** Green across the board; verify local web UI still works (no regression).

**Files:** none new; run suite.

**Step 1:** `PYTHONPATH= .venv/Scripts/python -m pytest tests/ -q --ignore=tests/unit --ignore=tests/integration -p no:cacheprovider` → expect **166+ passed, 0 failed**.

**Step 2:** Restart V2 (`python main.py`), `curl -X POST http://127.0.0.1:8000/chat` with a multi-step prompt ("现在时间，然后列出当前目录") → expect agent to call shell twice and return combined answer (not a one-liner).

**Step 3:** `git add -A && git reset -q .codegraph/daemon.pid && git commit -m "feat: LangGraph ReAct execution engine (multi-step, supervised, capped)" && git push origin main`

---

## Files Likely To Change
- `core/graph/__init__.py` (new), `core/graph/state.py` (new), `core/graph/contract.py` (new), `core/graph/nodes.py` (new), `core/graph/engine.py` (new)
- `core/agent.py` (`run_turn` rewrite, lines 89–111)
- `settings.py` (caps), `requirements.txt` (optional langgraph)
- Tests: `tests/test_graph_state.py`, `tests/test_graph_nodes.py`, `tests/test_graph_engine.py`, `tests/test_graph_caps.py`, extend `tests/integration/test_agent_flow.py`
- **Reused unchanged:** `core/supervisor.py`, `core/tool_executor.py`, `core/output_parser.py`, `tools/`, `runtime/task_queue.py`, `interface/web.py`, `interface/lark_ws.py`, `llm/fake.py`, `llm/base.py`

## Tests / Validation
- Unit: state, nodes (FakeLLM), engine (offline fallback + langraph path if installed), caps.
- Integration: `test_agent_flow.py` extended for multi-step.
- Manual: web UI multi-step prompt → 2 tool calls, combined answer.
- Full suite target: **166+ passed, 0 failed**.

## Risks / Tradeoffs / Open Questions
- **LangGraph install weight:** pulls `langchain-core` etc. Mitigated by lazy import + stdlib fallback so runtime/tests work offline; `langgraph` only needed for the "full" graph features. *Open question for user: hard-require langgraph or keep it optional?* Plan assumes **optional** (recommended for dep control).
- **Small-model ReAct stability:** nemotron-nano may still emit malformed steps. Mitigated by existing `output_parser` repair + Supervisor retry budget + `max_steps` cap (no infinite loops).
- **Supervisor lesson capture** writes to `pattern_store` — verify it doesn't double-write with existing `Curator`. Keep Supervisor's `memory=self.pattern_store`.
- **Backward compat:** `run_turn(text, *, user_id)` signature preserved → web/lark unchanged. This is the safest integration seam.
- **Scope discipline (YAGNI):** We do NOT rewrite `runtime/runtime_manager.py`/`skills/` in this plan — they're a larger persistence-layer refactor. The graph reuses Supervisor + ToolExecutor directly, delivering "strong long-task execution" without destabilizing goal persistence. Flag as follow-up.
