"""Graph-backed execution boundary for one user goal.

The Goal Runtime owns scheduling and the business Goal lifecycle. This module
owns only the LangGraph portion of execution: building the graph input,
running the graph, and returning its state to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.graph.engine import run_graph
from core.graph.state import AgentState
from core.supervisor import Supervisor


@dataclass(frozen=True)
class GraphExecutionRequest:
    """Inputs required to execute one Goal through LangGraph."""

    goal_id: str
    user_id: str
    text: str
    approval_token: str | None = None
    history: str = ""


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
            supervisor=self.supervisor,
            history=request.history,
            prompt_builder=self.prompt_builder,
            parser=self.output_parser,
            intent_classifier=self.intent_classifier,
            router=self.router,
            max_retry=self.max_retry,
            hitl=hitl,
        )
