"""Graph-backed execution boundary for one user goal.

The Goal Runtime owns scheduling and the business Goal lifecycle. This module
owns only the LangGraph portion of execution: building the graph input,
running the graph, and returning its state to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

from core.graph.engine import run_graph
from core.graph.state import AgentState
from core.supervisor import Supervisor
from core.operation_context import OperationContext, operation_context
from core.graph.contract import DECISION_FAIL
from core.graph.contract import DECISION_DONE
from core.capabilities import local_inventory_answer
from core.graph.nodes import persist_decision
from tools.registry import ToolRegistry


@dataclass(frozen=True)
class GraphExecutionRequest:
    """Inputs required to execute one Goal through LangGraph."""

    goal_id: str
    user_id: str
    text: str
    approval_token: str | None = None
    history: str = ""
    chat_id: str = ""
    request_id: str = ""


class GraphGoalExecutor:
    """Execute one Goal with the ReAct StateGraph.

    This class deliberately does not create or transition business Goals and
    does not send user notifications. Those responsibilities belong to the
    Goal Runtime/Worker layer. Keeping this boundary explicit allows the
    current message path and the future background Worker to share the same
    graph implementation.
    """

    def __init__(
        self,
        *,
        llm_client: Any,
        tool_registry: Any,
        tool_executor: Any,
        supervisor: Supervisor,
        prompt_builder: Any,
        output_parser: Any,
        intent_classifier: Any,
        router: Any,
        graph_db_path: str,
        max_steps: int = 12,
        max_retry: int = 2,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.supervisor = supervisor
        self.prompt_builder = prompt_builder
        self.output_parser = output_parser
        self.intent_classifier = intent_classifier
        self.router = router
        self.graph_db_path = graph_db_path
        self.max_steps = max(1, int(max_steps))
        self.max_retry = max(0, int(max_retry))

    async def execute(
        self,
        request: GraphExecutionRequest,
        *,
        hitl: bool = False,
    ) -> AgentState:
        """Run one graph invocation and return the final graph state."""
        context = OperationContext(
            goal_id=request.goal_id, request_id=request.request_id or request.goal_id,
            run_id=uuid.uuid4().hex, chat_id=request.chat_id,
        )
        with operation_context(context):
            store = getattr(self.tool_executor, "operation_store", None)
            if store is not None:
                try:
                    unresolved = await store.unresolved_writes(request.goal_id)
                except Exception:
                    return {"decision": DECISION_FAIL, "is_goal_complete": False,
                            "final_answer": "操作记录不可用，已停止执行，请人工检查。"}
                if unresolved:
                    return await persist_decision({
                        "decision": DECISION_FAIL, "is_goal_complete": False,
                        "decision_reason": "unreconciled_write_on_restart",
                        "final_answer": "此前操作的副作用尚未确认，已停止自动执行，请人工核对目标状态。",
                    }, store)
            return await self._execute_graph(request, hitl=hitl)

    async def _execute_graph(
        self, request: GraphExecutionRequest, *, hitl: bool,
    ) -> AgentState:
        seed: AgentState = {
            "goal": request.text,
            "user_id": request.user_id,
            "approval_token": request.approval_token,
            "messages": [],
            "scratchpad": [],
            "step_count": 0,
            "last_tool_result": None,
            "last_parsed": None,
            "decision": None,
            "final_answer": "",
            "is_goal_complete": False,
        }
        if isinstance(self.tool_registry, ToolRegistry):
            answer = local_inventory_answer(request.text, self.tool_registry)
            if answer is not None:
                return await persist_decision({**seed, "decision": DECISION_DONE, "final_answer": answer,
                                               "decision_reason": "local_capability_inventory",
                                               "is_goal_complete": True},
                                              getattr(self.tool_executor, "operation_store", None))
        config = {
            "configurable": {
                "thread_id": f"{request.user_id}:{request.goal_id}"
            }
        }
        return await run_graph(
            seed,
            config=config,
            max_steps=self.max_steps,
            db_path=self.graph_db_path,
            llm=self.llm_client,
            tools=self.tool_registry,
            executor=self.tool_executor,
            operation_store=getattr(self.tool_executor, "operation_store", None),
            supervisor=self.supervisor,
            history=request.history,
            prompt_builder=self.prompt_builder,
            parser=self.output_parser,
            intent_classifier=self.intent_classifier,
            router=self.router,
            max_retry=self.max_retry,
            hitl=hitl,
        )
