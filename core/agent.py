from __future__ import annotations

import asyncio
import json
from enum import Enum
from typing import Any

from core.intent_classifier import IntentClassifier
from core.output_parser import IntentType, OutputParser, ParseError, ParsedOutput
from core.prompt_builder import PromptBuilder
from core.result_summarizer import ResultSummarizer
from core.router import ToolRouter
from core.tool_executor import ToolExecutor
from memory.context_store import ContextStore
from memory.curator import Curator
from memory.goal_store import Goal, GoalStatus, GoalStore
from memory.pattern_store import PatternStore
from tools.registry import ToolRegistry


class AgentState(Enum):
    IDLE = "IDLE"
    ROUTING = "ROUTING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    AWAITING_RESULT = "AWAITING_RESULT"
    EVALUATING = "EVALUATING"
    DONE = "DONE"
    FAILED = "FAILED"


class MinimalAgent:
    """Minimal reliable agent loop with Phase 2 state persistence."""

    def __init__(
        self,
        *,
        llm_client: Any,
        tool_registry: ToolRegistry,
        intent_classifier: IntentClassifier | None = None,
        router: ToolRouter | None = None,
        goal_store: GoalStore | None = None,
        pattern_store: PatternStore | None = None,
        context_store: ContextStore | None = None,
        curator: Curator | None = None,
        curator_trigger_interval: int = 50,
        context_budget_total: int = 32_000,
        context_compress_threshold: float = 0.5,
        prompt_builder: PromptBuilder | None = None,
        output_parser: OutputParser | None = None,
        tool_executor: ToolExecutor | None = None,
        result_summarizer: ResultSummarizer | None = None,
        history_summary: str = "",
        experience_patterns: list[Any] | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.intent_classifier = intent_classifier or IntentClassifier()
        self.router = router or ToolRouter(tool_registry)
        self.goal_store = goal_store
        self.pattern_store = pattern_store
        self.context_store = context_store
        self.curator = curator or (
            Curator(pattern_store=pattern_store, llm_client=llm_client)
            if pattern_store is not None
            else None
        )
        self.curator_trigger_interval = curator_trigger_interval
        self.context_budget_total = context_budget_total
        self.context_compress_threshold = context_compress_threshold
        self.prompt_builder = prompt_builder or PromptBuilder(pattern_store=pattern_store)
        if pattern_store is not None and getattr(self.prompt_builder, "pattern_store", None) is None:
            self.prompt_builder.pattern_store = pattern_store
        repair_fn = getattr(llm_client, "repair", None)
        self.output_parser = output_parser or OutputParser(repair_fn=repair_fn)
        pattern_writer = getattr(pattern_store, "write_pattern", None)
        self.tool_executor = tool_executor or ToolExecutor(
            tool_registry,
            pattern_writer=pattern_writer,
        )
        self.result_summarizer = result_summarizer or ResultSummarizer()
        self.history_summary = history_summary
        self.experience_patterns = experience_patterns or []
        self.state = AgentState.IDLE
        self.conversation_history: list[dict[str, str]] = []
        self.completed_goal_count = 0
        self._background_tasks: list[asyncio.Task[Any]] = []

    async def run_turn(self, user_input: str, *, user_id: str = "default") -> str:
        self.state = AgentState.IDLE
        goal = await self._create_goal(user_id, user_input)
        intent = self.intent_classifier.classify(user_input)
        tools = self.router.route(user_input, intent)
        self._transition(goal, AgentState.ROUTING, intent_type=intent.value)
        self._transition(goal, AgentState.PLANNING)
        system_prompt = self.prompt_builder.build_system_prompt()
        history_summary = self._build_history_summary()
        await self._maybe_compress_context(user_id, user_input, system_prompt)
        task_prompt = await self.prompt_builder.build_task_prompt_with_experience_search(
            intent,
            tools,
            history_summary,
            user_input=user_input,
            experience_patterns=self.experience_patterns,
        )

        raw_output = await self.llm_client.generate(system_prompt, task_prompt)
        parsed = await self._parse_with_retry(raw_output)
        response = await self._respond(parsed, user_input, goal, intent, user_id)
        self._record_turn(user_input, response)
        return response

    def run_turn_sync(self, user_input: str) -> str:
        return asyncio.run(self.run_turn(user_input))

    async def _parse_with_retry(self, raw_output: str) -> ParsedOutput:
        try:
            return self.output_parser.parse(raw_output)
        except ParseError as exc:
            return await self.output_parser.repair_and_retry(raw_output, exc)

    async def _respond(
        self,
        parsed: ParsedOutput,
        user_input: str,
        goal: Goal | None,
        classified_intent: IntentType,
        user_id: str,
    ) -> str:
        if parsed.intent is IntentType.CHAT:
            self._transition(goal, AgentState.EVALUATING)
            self._transition(goal, AgentState.DONE, result=parsed.message)
            return parsed.message
        if parsed.intent is IntentType.CLARIFY:
            message = f"{parsed.question}\n如果你不回答，我将按此理解执行：{parsed.best_guess}"
            self._transition(goal, AgentState.EVALUATING)
            self._transition(goal, AgentState.DONE, result=message)
            return message
        if parsed.intent is IntentType.CANNOT_COMPLETE:
            message = f"无法完成：{parsed.reason}\n建议：{parsed.suggestion}"
            self._transition(goal, AgentState.FAILED, error=parsed.reason, result=message)
            return message

        if parsed.tool_call is None:
            message = "无法完成：模型没有提供工具调用。\n建议：请重试。"
            self._transition(goal, AgentState.FAILED, error="missing tool_call", result=message)
            return message

        self._transition(goal, AgentState.EXECUTING, plan=parsed.plan)
        execution_task = asyncio.create_task(
            self.tool_executor.execute(
                parsed.tool_call.name,
                parsed.tool_call.args,
                user_id=user_id,
            )
        )
        self._transition(goal, AgentState.AWAITING_RESULT)
        result = await execution_task
        if result.status == "error" and parsed.fallback:
            result.metadata.setdefault("fallback", parsed.fallback)
        self._transition(goal, AgentState.EVALUATING)
        tool_calls = [
            {
                "name": parsed.tool_call.name,
                "args": parsed.tool_call.args,
                "status": result.status,
                "error": result.error,
                "fallback": parsed.fallback,
            }
        ]
        if result.status != "ok":
            return await self._failed_action_summary(
                result,
                user_input,
                goal,
                tool_calls,
            )
        message = await self.result_summarizer.summarize(
            result,
            user_intent=user_input,
            user_language=self._detect_language(user_input),
        )
        self._transition(
            goal,
            AgentState.DONE,
            tool_calls=tool_calls,
            result=message,
        )
        return message

    async def _failed_action_summary(
        self,
        result: Any,
        user_input: str,
        goal: Goal | None,
        tool_calls: list[dict[str, Any]],
    ) -> str:
        message = await self.result_summarizer.summarize(
            result,
            user_intent=user_input,
            user_language=self._detect_language(user_input),
        )
        fallback = tool_calls[0].get("fallback") if tool_calls else ""
        if fallback:
            message = f"{message}\nFallback: {fallback}"
        self._transition(
            goal,
            AgentState.FAILED,
            tool_calls=tool_calls,
            error=result.error or "",
            result=message,
        )
        return message

    def _detect_language(self, text: str) -> str:
        for char in text:
            if "\u4e00" <= char <= "\u9fff":
                return "zh"
        return "en"

    async def _create_goal(self, user_id: str, user_input: str) -> Goal | None:
        if self.goal_store is None:
            return None
        return await asyncio.create_task(self.goal_store.create(user_id, user_input))

    def _transition(
        self,
        goal: Goal | None,
        state: AgentState,
        **kwargs: Any,
    ) -> None:
        self.state = state
        if goal is None or self.goal_store is None:
            return
        goal_status = GoalStatus(state.value)
        if "tool_calls" in kwargs and not isinstance(kwargs["tool_calls"], str):
            kwargs["tool_calls"] = json.dumps(kwargs["tool_calls"], ensure_ascii=False)
        self.goal_store.schedule_status_update(goal.id, goal_status, **kwargs)
        if state is AgentState.DONE:
            self._maybe_trigger_curator()

    async def _maybe_compress_context(
        self,
        user_id: str,
        user_input: str,
        system_prompt: str,
    ) -> None:
        if self.context_store is None or len(self.conversation_history) <= 3:
            return
        text = "\n".join(
            [system_prompt, self.history_summary, user_input]
            + [turn.get("content", "") for turn in self.conversation_history]
        )
        if self._estimate_tokens(text) <= self.context_budget_total * self.context_compress_threshold:
            return
        tail_count = 3
        middle = self.conversation_history[:-tail_count]
        if not middle:
            return
        middle_text = "\n".join(f"{turn.get('role', '')}: {turn.get('content', '')}" for turn in middle)
        summary = await self.llm_client.generate(
            "You compress conversation history for luck-agent.",
            "Compress the middle conversation history to 200 tokens or fewer.\n\n"
            + middle_text,
        )
        self.history_summary = summary.strip()
        turn_range = {"from": 1, "to": len(middle)}
        task = asyncio.create_task(
            self.context_store.save_summary(
                user_id=user_id,
                summary=self.history_summary,
                turn_range=turn_range,
            )
        )
        self._track_background_task(task)
        self.conversation_history = self.conversation_history[-tail_count:]

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _build_history_summary(self) -> str:
        # Feed recent conversation turns back into the prompt so the agent
        # keeps multi-turn context (previously every turn was treated as a
        # fresh conversation because only the compressed `history_summary`
        # was passed, which starts empty).
        recent = self.conversation_history[-6:]
        if not recent and not self.history_summary:
            return ""
        parts: list[str] = []
        if self.history_summary:
            parts.append(f"[earlier summary]\n{self.history_summary}")
        if recent:
            turns = "\n".join(
                f"{turn.get('role', '')}: {turn.get('content', '')}" for turn in recent
            )
            parts.append(f"[recent]\n{turns}")
        return "\n\n".join(parts)

    def _record_turn(self, user_input: str, response: str) -> None:
        self.conversation_history.append({"role": "user", "content": user_input})
        self.conversation_history.append({"role": "assistant", "content": response})

    def _maybe_trigger_curator(self) -> None:
        if self.curator is None or self.curator_trigger_interval <= 0:
            return
        self.completed_goal_count += 1
        if self.completed_goal_count % self.curator_trigger_interval != 0:
            return
        task = asyncio.create_task(self.curator.run())
        self._track_background_task(task)

    def _track_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.append(task)
        task.add_done_callback(lambda done: self._remove_background_task(done))

    def _remove_background_task(self, task: asyncio.Task[Any]) -> None:
        try:
            self._background_tasks.remove(task)
        except ValueError:
            pass

    async def drain_background_tasks(self) -> None:
        if self.goal_store is not None:
            await self.goal_store.drain_pending()
        await self.tool_executor.drain_pending_patterns()
        while self._background_tasks:
            await asyncio.gather(*list(self._background_tasks))
