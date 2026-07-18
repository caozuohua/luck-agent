from __future__ import annotations

from pathlib import Path
from typing import Any

from core.output_parser import IntentType
from memory.pattern_store import PatternStore
from tools.base import Tool


GLOBAL_BEHAVIOR_RULES = """## 全局输出格式规范
- 所有输出必须是单个 JSON 对象。
- ACTION 输出必须包含 intent、plan、tool_call、fallback。
- CHAT 输出必须包含 intent、message。
- CLARIFY 输出必须包含 intent、question、best_guess。
- CANNOT_COMPLETE 输出必须包含 intent、reason、suggestion。

## 全局行为规则
鼓励：
- 优先调用工具获取实时信息，再回答
- 对复杂任务拆分步骤，逐步执行
- 不确定时使用逃生阀，不猜测

禁止：
- 禁止在未调用工具的情况下声称已执行操作
- 禁止连续追问超过 1 次
- 禁止将原始 JSON 数据直接返回用户
"""


class PromptBuilder:
    def __init__(
        self,
        *,
        soul_path: str | Path | None = None,
        memory_path: str | Path | None = None,
        pattern_store: PatternStore | None = None,
        history_token_budget: int = 3000,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        self.soul_path = Path(soul_path) if soul_path else root / "soul" / "SOUL.md"
        self.memory_path = Path(memory_path) if memory_path else root / "soul" / "MEMORY.md"
        self.pattern_store = pattern_store
        self.history_token_budget = history_token_budget

    def build_system_prompt(self) -> str:
        soul = self._read_required(self.soul_path)
        memory = self._read_optional(self.memory_path)
        memory_block = memory if memory else "暂无长期记忆。"
        return "\n\n".join(
            [
                "# Layer 1: System Prompt",
                soul,
                "## 长期记忆",
                memory_block,
                GLOBAL_BEHAVIOR_RULES,
            ]
        ).strip()

    def build_task_prompt(
        self,
        intent: IntentType,
        tool_subset: list[Tool],
        history_summary: str,
        experience_patterns: list[Any],
        user_input: str = "",
    ) -> str:
        task_context = user_input or intent.value
        tool_docs = [
            self.get_tool_docstring(tool, task_context, experience_patterns)
            for tool in tool_subset
        ]
        patterns = self._format_patterns(experience_patterns)
        return "\n\n".join(
            [
                "# Layer 2: Task Prompt",
                f"Current intent: {intent.value}",
                f"Current user input: {user_input or '(not provided)'}",
                "## Available tools",
                "\n\n".join(tool_docs) if tool_docs else "No tools are available.",
                "## History summary",
                self._fit_history(history_summary),
                "## Related experience patterns",
                patterns,
                "# Required response",
                "Return exactly one JSON object matching the current intent schema.",
            ]
        ).strip()

    async def build_task_prompt_with_experience_search(
        self,
        intent: IntentType,
        tool_subset: list[Tool],
        history_summary: str,
        user_input: str,
        experience_patterns: list[Any] | None = None,
    ) -> str:
        patterns = list(experience_patterns or [])
        if self.pattern_store is not None:
            patterns = await self.pattern_store.search_patterns(user_input, limit=3)
        return self.build_task_prompt(
            intent,
            tool_subset,
            history_summary,
            patterns,
            user_input=user_input,
        )

    def get_tool_docstring(
        self,
        tool: Tool,
        task_context: str,
        experience_patterns: list[Any] | None = None,
    ) -> str:
        experience = self._format_patterns(experience_patterns or [])
        return tool.docstring(task_context=task_context, experience=experience)

    def _read_required(self, path: Path) -> str:
        if not path.exists():
            raise FileNotFoundError(f"required prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def _read_optional(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _format_patterns(self, patterns: list[Any]) -> str:
        if not patterns:
            return "No related patterns."
        lines = []
        for pattern in patterns[:3]:
            if isinstance(pattern, dict):
                trigger = str(pattern.get("trigger", "")).strip()
                outcome = str(pattern.get("outcome", "")).strip()
                tool_name = str(pattern.get("tool_name", "")).strip()
                lines.append(f"- [{tool_name or 'pattern'}] {trigger} -> {outcome}".strip())
            else:
                lines.append(f"- {pattern}")
        return "\n".join(lines)

    def _fit_history(self, history_summary: str) -> str:
        history = history_summary.strip()
        if not history:
            return "No prior history."
        max_chars = max(0, self.history_token_budget * 4)
        if len(history) <= max_chars:
            return history
        if max_chars <= 20:
            return "[truncated] " + history[-max_chars:]
        return "[truncated] " + history[-max_chars:]
