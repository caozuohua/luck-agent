from __future__ import annotations

import asyncio
import unittest

from core.agent import MinimalAgent
from core.output_parser import IntentType, OutputParser, ParseError
from core.tool_executor import ToolExecutor
from tools.base import Tool, ToolResult
from tools.registry import ToolRegistry


class EchoTool(Tool):
    name = "echo"
    description = "Echo a text value."
    args_schema = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult.ok(data={"echo": kwargs.get("text", "")}, tool_name=self.name)


class SlowTool(Tool):
    name = "slow"
    description = "Sleep longer than the executor timeout."

    async def run(self, **kwargs: object) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult.ok(data={"done": True}, tool_name=self.name)


class FakeLLM:
    async def generate(self, system_prompt: str, task_prompt: str) -> str:
        return """
        ```json
        {
          "intent": "ACTION",
          "plan": "echo the requested text",
          "tool_call": {"name": "echo", "args": {"text": "hello"}},
          "fallback": "return a clear failure message"
        }
        ```
        """


class Phase1ParserTests(unittest.TestCase):
    def test_parser_accepts_markdown_wrapped_action_json(self) -> None:
        parsed = OutputParser().parse(
            """```json
            {"intent":"ACTION","plan":"say hi","tool_call":{"name":"echo","args":{"text":"hi"}},"fallback":"fail"}
            ```"""
        )

        self.assertEqual(parsed.intent, IntentType.ACTION)
        self.assertEqual(parsed.tool_call.name, "echo")
        self.assertEqual(parsed.tool_call.args, {"text": "hi"})

    def test_parser_accepts_chat_schema(self) -> None:
        parsed = OutputParser().parse(
            '{"intent":"CHAT","message":"hello there"}'
        )

        self.assertEqual(parsed.intent, IntentType.CHAT)
        self.assertEqual(parsed.message, "hello there")

    def test_parser_accepts_clarify_schema(self) -> None:
        parsed = OutputParser().parse(
            '{"intent":"CLARIFY","question":"Which file?","best_guess":"Use README.md"}'
        )

        self.assertEqual(parsed.intent, IntentType.CLARIFY)
        self.assertEqual(parsed.question, "Which file?")
        self.assertEqual(parsed.best_guess, "Use README.md")

    def test_parser_accepts_cannot_complete_schema(self) -> None:
        parsed = OutputParser().parse(
            '{"intent":"CANNOT_COMPLETE","reason":"missing permission","suggestion":"grant access"}'
        )

        self.assertEqual(parsed.intent, IntentType.CANNOT_COMPLETE)
        self.assertEqual(parsed.reason, "missing permission")
        self.assertEqual(parsed.suggestion, "grant access")

    def test_parser_rejects_missing_chat_message(self) -> None:
        with self.assertRaises(ParseError):
            OutputParser().parse('{"intent":"CHAT"}')

    def test_parser_rejects_action_without_tool_call_name(self) -> None:
        with self.assertRaisesRegex(ParseError, "tool_call.name"):
            OutputParser().parse(
                '{"intent":"ACTION","plan":"do it","tool_call":{"args":{}},"fallback":"stop"}'
            )

    def test_parser_rejects_action_with_non_object_args(self) -> None:
        with self.assertRaisesRegex(ParseError, "tool_call.args"):
            OutputParser().parse(
                '{"intent":"ACTION","plan":"do it","tool_call":{"name":"echo","args":[]},"fallback":"stop"}'
            )

    def test_parser_rejects_unknown_intent(self) -> None:
        with self.assertRaisesRegex(ParseError, "unsupported intent"):
            OutputParser().parse('{"intent":"DONE","message":"ok"}')

    def test_parser_repair_retries_invalid_output(self) -> None:
        async def repair(raw_output: str, error: ParseError, attempt: int) -> str:
            self.assertEqual(attempt, 1)
            self.assertIn("not-json", raw_output)
            self.assertIn("invalid JSON", str(error))
            return '{"intent":"CHAT","message":"repaired"}'

        async def run() -> None:
            parser = OutputParser(repair_fn=repair)
            parsed = await parser.repair_and_retry("not-json", ParseError("invalid JSON"))
            self.assertEqual(parsed.intent, IntentType.CHAT)
            self.assertEqual(parsed.message, "repaired")

        asyncio.run(run())

    def test_parser_repair_uses_max_retries_then_cannot_complete(self) -> None:
        attempts: list[int] = []

        async def repair(raw_output: str, error: ParseError, attempt: int) -> str:
            attempts.append(attempt)
            return '{"intent":"CHAT"}'

        async def run() -> None:
            parser = OutputParser(repair_fn=repair, max_retries=2)
            parsed = await parser.repair_and_retry("not-json", ParseError("invalid JSON"))
            self.assertEqual(attempts, [1, 2])
            self.assertEqual(parsed.intent, IntentType.CANNOT_COMPLETE)
            self.assertIn("message is required", parsed.reason)

        asyncio.run(run())

    def test_parser_without_repair_fn_degrades_to_cannot_complete(self) -> None:
        async def run() -> None:
            parser = OutputParser(max_retries=2)
            parsed = await parser.repair_and_retry("not-json", ParseError("invalid JSON"))
            self.assertEqual(parsed.intent, IntentType.CANNOT_COMPLETE)
            self.assertIn("invalid JSON", parsed.reason)

        asyncio.run(run())


class Phase1ToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_runs_registered_tool(self) -> None:
        registry = ToolRegistry([EchoTool()])

        result = await ToolExecutor(registry).execute("echo", {"text": "ok"})

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data, {"echo": "ok"})
        self.assertEqual(result.metadata["tool_name"], "echo")

    async def test_executor_returns_timeout_result(self) -> None:
        registry = ToolRegistry([SlowTool()])

        result = await ToolExecutor(registry, timeout_seconds=0.01).execute("slow", {})

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "TIMEOUT_ERROR")
        self.assertEqual(result.metadata["tool_name"], "slow")


class Phase1SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_action_loop_calls_tool_and_summarizes(self) -> None:
        registry = ToolRegistry([EchoTool()])
        agent = MinimalAgent(
            llm_client=FakeLLM(),
            tool_registry=registry,
            history_summary="",
            experience_patterns=[],
        )

        response = await agent.run_turn("please echo hello")

        self.assertIn("hello", response)
        self.assertNotIn('"status"', response)


if __name__ == "__main__":
    unittest.main()
