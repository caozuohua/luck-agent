# LangGraph ReAct Execution Engine (LangGraph-native) — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the single-call `MinimalAgent.run_turn` with a **LangGraph-native, production-grade ReAct graph** (Think→Act→Observe→Supervise, looping) that fully uses LangGraph's capabilities: `StateGraph` with typed state, a **checkpointer for durable long-task state + resume**, **`interrupt()` human-in-the-loop** on risky steps, and **`recursion_limit`** as the hard step cap. Reuse the *already-present* `core/supervisor.py` (verify/retry/block) and `core/tool_executor.py` as the tool/verify layer inside the graph.

**Architecture:** A compiled LangGraph `StateGraph` with nodes `planner → executor → supervisor → (router) → planner | responder`. The Supervisor's `block` decision triggers `interrupt()` (LangGraph HITL); the graph is driven by a **checkpointer** (SQLite) so a long task survives process restart and can be resumed with `graph.ainvoke(None, config)`. `run_turn` becomes "compile graph once, `ainvoke` with a `thread_id`, return final string" — non-breaking to `interface/web.py` / `interface/lark_ws.py`.

**Tech Stack:** Python 3.12, **`langgraph>=0.2` (HARD dependency)** + `langchain-core`, existing `aiosqlite`/`httpx`. `llm/base.py` `FakeLLMClient` for offline unit tests (no network). Checkpointer = `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` backed by a local `graph_state.db` (or reuse the agent sqlite).

---

## Current Context / Findings (verified by reading the repo)

- `core/agent.py::MinimalAgent.run_turn` (89–111) = **ONE** LLM call + **ONE** `tool_executor.execute`, no loop. Root cause of "agent does nothing."
- `core/supervisor.py` **exists**: `review_step_result()` → `pass|retry|block|fail` + lesson capture. Not called by `run_turn` today — we wire it in as the `supervisor` node.
- `core/tool_executor.py` has `timeout_seconds` (30) + command→shell aliasing — reuse as `executor` node.
- `runtime/task_queue.py` `RuntimeTaskQueue(max_active=1)` — reuse for the concurrency cap (one active graph per user).
- `skills/legacy_react.py` = empty stub (old ReAct removed, never replaced).
- `langgraph` is **NOT installed** (`ModuleNotFoundError`) → Task 0 installs it.
- `interface/web.py` / `interface/lark_ws.py` call **only** `agent.run_turn(text, *, user_id)` → keep signature.

**Resource controls (explicit user ask — "控制资源占用和依赖"):**
- `recursion_limit` = `max_steps` (default 12) — LangGraph hard cap on loop iterations (no infinite loops).
- `max_retry` (default 2) — enforced by Supervisor `retry_count < max_retry`.
- `max_active=1` — one concurrent graph per user via `RuntimeTaskQueue` (existing).
- `step_timeout` — per-tool, already in `ToolExecutor`.
- `context_budget_total` (32k) — preserved in `MinimalAgent`, fed into planner prompt.
- Checkpointer keeps state **out of RAM** → long tasks don't bloat memory.

---

## Proposed Approach — LangGraph-native

`core/graph/` package:
- `state.py` — `AgentState(TypedDict)`: `goal`, `user_id`, `messages` (LLM message list), `scratchpad` (observations), `step_count`, `last_tool_result`, `decision`, `final_answer`, `is_goal_complete`.
- `nodes.py` — `planner`, `executor`, `supervisor`, `responder` async node functions. `planner` builds the ReAct prompt (reuse `PromptBuilder`), calls `llm.generate`, parses (reuse `OutputParser`), appends to `messages`/`scratchpad`. `executor` calls `ToolExecutor.execute`. `supervisor` calls `Supervisor.review_step_result` and may `interrupt()` on `block`. `responder` formats the final answer.
- `engine.py` — `build_graph()` compiles the `StateGraph` with conditional edges + `AsyncSqliteSaver` checkpointer; `run_graph(state, *, config)` does `graph.ainvoke(initial, config, recursion_limit=...)`. Exposes `resume(thread_id, approval)` for HITL.
- `contract.py` — ReAct LLM output schema (extends `output_parser`): `intent` (ACTION/CHAT/DONE/CLARIFY/CANNOT_COMPLETE), `plan`, `tool_call{name,args}`, `is_goal_complete: bool`, `message`.

LangGraph features we **explicitly use** (the "fully leverage" part):
1. **`StateGraph` + typed `AgentState`** — structured, reducer-merged message state.
2. **`AsyncSqliteSaver` checkpointer** — durable, resumable long tasks (survive restart).
3. **`interrupt()` / `Command(resume=...)`** — Supervisor `block` → pause for human approval, resume without re-running past steps (production HITL).
4. **`recursion_limit`** — hard `max_steps` cap (raises `GraphRecursionError` if exceeded → mapped to CANNOT_COMPLETE).
5. **Conditional edges** (`supervisor` → `planner` on pass/retry, → `responder`/`END` on done/block/fail).
6. **`thread_id` per goal** — isolates concurrent users; pairs with `max_active=1` queue.

---

## Step-by-Step Plan

### Task 0: Install & verify LangGraph

**Objective:** Make `langgraph` available (hard dep).

**Files:** `requirements.txt`

**Step 1:** `uv pip install "langgraph>=0.2"` (or `pip install`).
**Step 2:** Verify — `.venv/Scripts/python -c "import langgraph, langgraph.checkpoint.sqlite; print(langgraph.__version__)"` → prints version, no error.
**Step 3:** Add to `requirements.txt`:
```
# ─── Graph engine (LangGraph, HARD dependency) ──────────────
langgraph>=0.2
```
**Step 4:** `git add requirements.txt && git commit -m "build: add langgraph hard dependency"`

---

### Task 1: Define the LangGraph AgentState + contract

**Objective:** Typed state the graph carries; ReAct output schema.

**Files:** Create `core/graph/__init__.py`, `core/graph/state.py`, `core/graph/contract.py`; Test `tests/test_graph_state.py`

**Step 1: Failing test**
```python
from core.graph.state import AgentState
def test_state_defaults():
    s: AgentState = {"goal": "list /tmp", "user_id": "u1"}
    assert s["step_count"] == 0 and s["scratchpad"] == [] and s["decision"] is None
```

**Step 2:** Run → FAIL (module missing).

**Step 3: Implement**
```python
# core/graph/__init__.py
from core.graph.engine import build_graph, run_graph, resume_graph

# core/graph/state.py
from __future__ import annotations
from typing import Any, TypedDict

class AgentState(TypedDict, total=False):
    goal: str
    user_id: str
    messages: list[dict[str, Any]]      # LLM message history
    scratchpad: list[dict[str, str]]     # thought/observation log
    step_count: int
    last_tool_result: dict[str, Any] | None
    last_parsed: dict[str, Any] | None
    decision: str | None                 # pass|retry|block|fail|done
    final_answer: str
    is_goal_complete: bool
```

```python
# core/graph/contract.py
REACT_SYSTEM_HINT = (
    "You run in a ReAct loop. Return ONE JSON object each turn: "
    "{\"intent\":\"ACTION|CHAT|DONE|CLARIFY|CANNOT_COMPLETE\", "
    "\"plan\":str, \"tool_call\":{\"name\":str,\"args\":{}}, "
    "\"is_goal_complete\":bool, \"message\":str}. "
    "Set is_goal_complete=true only when the goal is fully done."
)
```

**Step 4:** Run → PASS. **Step 5:** commit `feat(graph): LangGraph AgentState + ReAct contract`

---

### Task 2: Implement the four graph nodes

**Objective:** `planner`, `executor`, `supervisor`, `responder` async nodes.

**Files:** Create `core/graph/nodes.py`; Test `tests/test_graph_nodes.py` (offline via `FakeLLMClient`)

**Step 1: Failing test**
```python
from core.graph.nodes import planner, executor
from core.graph.state import AgentState
from llm.fake import FakeLLMClient
from tools.registry import ToolRegistry

def test_planner_appends_message():
    reg = ToolRegistry(); reg.register_builtin_tools()
    s: AgentState = {"goal":"time?", "messages":[], "scratchpad":[], "step_count":0}
    out = asyncio.run(planner(s, llm=FakeLLMClient(), tools=reg.list(), history=""))
    assert out["step_count"] == 1 and out["messages"]
```

**Step 2:** Run → FAIL. **Step 3: Implement (abridged; full in PR)**
```python
# core/graph/nodes.py
from __future__ import annotations
import asyncio, json
from typing import Any
from core.output_parser import OutputParser, ParseError
from core.tool_executor import ToolExecutor
from core.supervisor import Supervisor
from langgraph.types import interrupt

async def planner(state: dict, *, llm, tools, history, prompt_builder, parser, intent_classifier, router) -> dict:
    system = prompt_builder.build_system_prompt()
    task = await prompt_builder.build_task_prompt_with_experience_search(...)
    raw = await llm.generate(system, task)
    parsed = await _safe_parse(parser, raw)
    state["last_parsed"] = parsed.__dict__ if hasattr(parsed,"__dict__") else dict(parsed)
    state["messages"].append({"role":"assistant","content":raw})
    state["scratchpad"].append({"role":"thought","content":raw})
    state["step_count"] = state.get("step_count",0)+1
    return state

async def executor(state: dict, *, tools: ToolExecutor) -> dict:
    parsed = state.get("last_parsed") or {}
    tc = parsed.get("tool_call")
    if not tc:
        state["decision"] = "done" if parsed.get("intent") in ("CHAT","DONE") else "fail"
        return state
    result = await tools.execute(tc.get("name",""), tc.get("args",{}), user_id=state.get("user_id","default"))
    rd = result.to_dict()
    state["last_tool_result"] = rd
    state["scratchpad"].append({"role":"observation","content":json.dumps(rd, ensure_ascii=False)})
    return state

async def supervisor(state: dict, *, supervisor: Supervisor, goal: dict, max_retry: int) -> dict:
    parsed = state.get("last_parsed") or {}
    if parsed.get("intent") in ("CHAT","DONE") and parsed.get("is_goal_complete"):
        state["decision"] = "done"; state["final_answer"] = parsed.get("message",""); return state
    if state.get("last_tool_result") is None:
        state["decision"] = "done"; return state
    dec = supervisor.review_step_result(goal=goal, step={}, result=_wrap(state["last_tool_result"]),
                                         retry_count=state.get("step_count",0), max_retry=max_retry)
    state["decision"] = dec.decision
    if dec.decision == "block":
        # LangGraph HITL: pause for human approval, resume later
        approval = interrupt({"question": dec.reason, "decision": "block"})
        if not approval.get("approve"):
            state["decision"] = "fail"; state["final_answer"] = "Blocked by operator."
    return state

async def responder(state: dict) -> dict:
    if not state.get("final_answer"):
        state["final_answer"] = state["scratchpad"][-1].get("content","") if state.get("scratchpad") else "(no answer)"
    return state
```

**Step 4:** Run → PASS. **Step 5:** commit `feat(graph): planner/executor/supervisor/responder nodes`

---

### Task 3: Build the LangGraph graph (StateGraph + edges + checkpointer + recursion_limit)

**Objective:** Compile the production-grade graph.

**Files:** Create `core/graph/engine.py`; Test `tests/test_graph_engine.py`

**Step 1: Failing test** (offline; FakeLLM; asserts multi-step + termination)
```python
from core.graph.engine import build_graph, run_graph
def test_graph_runs_multi_step_offline():
    g = build_graph()
    from llm.fake import FakeLLMClient
    from tools.registry import ToolRegistry
    reg = ToolRegistry(); reg.register_builtin_tools()
    cfg = {"configurable": {"thread_id": "t1"}}
    out = asyncio.run(run_graph({"goal":"hi","user_id":"u1","messages":[],"scratchpad":[],"step_count":0},
                                graph=g, llm=FakeLLMClient(), tools=reg, supervisor=Supervisor(), history="", config=cfg, max_steps=5))
    assert isinstance(out["final_answer"], str)
```

**Step 2:** Run → FAIL. **Step 3: Implement**
```python
# core/graph/engine.py
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from typing import Any
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

def build_graph():
    from core.graph.nodes import planner, executor, supervisor, responder
    g = StateGraph(AgentState := __import__("core.graph.state", fromlist=["AgentState"]).AgentState)
    g.add_node("planner", planner); g.add_node("executor", executor)
    g.add_node("supervisor", supervisor); g.add_node("responder", responder)
    g.set_entry_point("planner")
    g.add_edge("planner","executor")
    g.add_edge("executor","supervisor")
    def route(state):
        d = state.get("decision")
        if d in ("done","fail"): return "responder"
        if d == "block": return "responder"   # block already handled via interrupt inside node
        return "planner"                       # pass | retry -> loop
    g.add_conditional_edges("supervisor", route)
    g.add_edge("responder", END)
    return g

@asynccontextmanager
async def _saver(path="graph_state.db"):
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        yield saver

async def run_graph(state, *, graph, llm, tools, supervisor, history, config, max_steps=12, db_path="graph_state.db"):
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        app = graph.compile(checkpointer=saver, recursion_limit=max_steps)
        result = await app.ainvoke(state, config, recursion_limit=max_steps)
        # recursion_limit hit -> GraphRecursionError -> map to CANNOT_COMPLETE
        return result

async def resume_graph(graph, thread_id, approval, *, db_path="graph_state.db", max_steps=12):
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        app = graph.compile(checkpointer=saver, recursion_limit=max_steps)
        return await app.ainvoke(Command(resume=approval), {"configurable":{"thread_id":thread_id}})
```
*(Wrap `ainvoke` in try/except `GraphRecursionError` → set `final_answer="步骤超出上限"`; import `Command` from `langgraph.types`.)*

**Step 4:** Run → PASS. **Step 5:** commit `feat(graph): compile StateGraph + SqliteSaver + recursion_limit`

---

### Task 4: Wire engine into MinimalAgent.run_turn (non-breaking)

**Objective:** `run_turn` delegates to the graph; web/Lark untouched.

**Files:** Modify `core/agent.py:89-111`; extend `tests/integration/test_agent_flow.py`

**Step 1: Failing test** — add a multi-step goal to `test_agent_flow.py` (requires 2 tool calls). Run → current single-call code can't.

**Step 2: Implement**
```python
async def run_turn(self, user_input, *, user_id="default") -> str:
    self.state = AgentState.IDLE
    goal = await self._create_goal(user_id, user_input)
    history = self._build_history_summary()
    graph = build_graph()
    init: AgentState = {"goal": user_input, "user_id": user_id,
                        "messages": [], "scratchpad": [], "step_count": 0,
                        "is_goal_complete": False}
    config = {"configurable": {"thread_id": f"{user_id}:{goal.id if goal else user_input}"}}
    try:
        out = await run_graph(init, graph=graph, llm=self.llm_client, tools=self.tool_registry,
                              supervisor=self._supervisor(), history=history, config=config,
                              max_steps=self.max_steps)
    except Exception as exc:
        out = {"final_answer": f"（处理出错：{exc}）"}
    answer = out.get("final_answer") or ""
    self._record_turn(user_input, answer)
    return answer

def _supervisor(self):
    return Supervisor(memory=self.pattern_store)
```

**Step 3:** Run `pytest tests/integration/test_agent_flow.py` → PASS. **Step 4:** commit `refactor(agent): run_turn delegates to LangGraph`

---

### Task 5: Resource caps + HITL wiring + persistence check

**Objective:** Enforce caps, wire `RuntimeTaskQueue(max_active=1)` in `main.py`, verify checkpointer persists.

**Files:** Modify `settings.py` (add `max_steps`, `max_retry`, `graph_db_path`), `main.py` (wrap goal start in `RuntimeTaskQueue`), `core/graph/engine.py` (GraphRecursionError handling); Test `tests/test_graph_caps.py`

**Step 1: Failing test** — `test_recursion_limit_stops`: FakeLLM always ACTION, `max_steps=2` → `run_graph` returns without hanging; `test_checkpointer_resume`: after `interrupt` (block), `resume_graph` continues.

**Step 2: Implement caps** — `recursion_limit=max_steps` already in `run_graph`; `supervisor` node `block`→`interrupt()`; `main.py` submits each `run_turn` through `RuntimeTaskQueue(max_active=settings.graph_max_active or 1)` so only one graph executes per user at a time.

**Step 3:** `settings.py`:
```python
max_steps: int = 12
max_retry: int = 2
graph_db_path: str = "graph_state.db"
graph_max_active: int = 1
```
load from `MAX_STEPS`, `MAX_RETRY`, `GRAPH_DB_PATH`, `GRAPH_MAX_ACTIVE`.

**Step 4:** Run `pytest tests/test_graph_caps.py` → PASS. **Step 5:** commit `feat(graph): caps, HITL interrupt, durable checkpointer`

---

### Task 6: Full suite + web smoke + push

**Objective:** Green; verify local web UI multi-step works; push.

**Files:** none new.

**Step 1:** `PYTHONPATH= .venv/Scripts/python -m pytest tests/ -q --ignore=tests/unit --ignore=tests/integration -p no:cacheprovider` → **166+ passed, 0 failed**.
**Step 2:** Restart V2 (`python main.py`); `curl -X POST http://127.0.0.1:8000/chat -d '{"text":"现在时间，然后列出当前目录"}'` → agent calls `date` then `pwd` (2 tool calls), returns combined answer.
**Step 3:** `git add -A && git reset -q .codegraph/daemon.pid && git commit -m "feat: LangGraph-native ReAct engine (checkpointer, HITL, capped)" && git push origin main`

---

## Files Likely To Change
- **New:** `core/graph/__init__.py`, `core/graph/state.py`, `core/graph/contract.py`, `core/graph/nodes.py`, `core/graph/engine.py`
- **Modify:** `core/agent.py` (`run_turn`), `settings.py` (caps), `requirements.txt` (langgraph hard dep), `main.py` (task-queue guard)
- **Tests:** `tests/test_graph_state.py`, `tests/test_graph_nodes.py`, `tests/test_graph_engine.py`, `tests/test_graph_caps.py`, extend `tests/integration/test_agent_flow.py`
- **Reused unchanged:** `core/supervisor.py`, `core/tool_executor.py`, `core/output_parser.py`, `tools/`, `runtime/task_queue.py`, `interface/web.py`, `interface/lark_ws.py`, `llm/fake.py`, `llm/base.py`

## Tests / Validation
- Unit (offline, FakeLLM): state, nodes, engine (multi-step + recursion_limit + checkpointer resume).
- Integration: `test_agent_flow.py` extended for multi-step + HITL.
- Manual: web UI multi-step prompt → 2 tool calls, combined answer.
- Full-suite target: **166+ passed, 0 failed**.

## Risks / Tradeoffs / Open Questions
- **Install weight:** `langgraph>=0.2` pulls `langchain-core` (~tens of MB). Accepted per user's "用 langgraph，充分发挥其能力" — hard dep.
- **Small-model ReAct stability:** nemotron-nano may emit malformed steps; mitigated by `output_parser` repair + Supervisor retry + `recursion_limit` (no infinite loop) + `CANNOT_COMPLETE` fallback.
- **Checkpointer DB:** `graph_state.db` (SQLite) created locally; gitignore it (add to `.gitignore`). On VPS deploy, point `GRAPH_DB_PATH` to the agent data dir.
- **HITL in web UI:** `interrupt()` pauses; the web `/chat` should return a "blocked, awaiting approval" reply and expose an approval endpoint (`POST /approve`). Task 6 manual test covers the non-block path; the `/approve` endpoint is a **follow-up** (note in commit message) to keep this plan scoped.
- **Backward compat:** `run_turn(text, *, user_id)` preserved → web/lark unchanged.
- **Scope (YAGNI):** Not rewriting `runtime/runtime_manager.py`/`skills/` — larger persistence-layer refactor; graph reuses Supervisor+ToolExecutor directly. Flagged as follow-up.
