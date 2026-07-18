from __future__ import annotations

import pytest

from core.output_parser import IntentType, OutputParser, ParseError


def test_parse_valid_action_schema() -> None:
    parsed = OutputParser().parse(
        '{"intent":"ACTION","plan":"do it","tool_call":{"name":"echo","args":{"text":"hi"}},"fallback":"stop"}'
    )

    assert parsed.intent is IntentType.ACTION
    assert parsed.tool_call is not None
    assert parsed.tool_call.name == "echo"
    assert parsed.tool_call.args == {"text": "hi"}


def test_parse_valid_chat_schema() -> None:
    parsed = OutputParser().parse('{"intent":"CHAT","message":"hello"}')

    assert parsed.intent is IntentType.CHAT
    assert parsed.message == "hello"


def test_parse_valid_clarify_schema() -> None:
    parsed = OutputParser().parse(
        '{"intent":"CLARIFY","question":"Which file?","best_guess":"README.md"}'
    )

    assert parsed.intent is IntentType.CLARIFY
    assert parsed.question == "Which file?"
    assert parsed.best_guess == "README.md"


def test_parse_markdown_wrapped_json() -> None:
    parsed = OutputParser().parse(
        '```json\n{"intent":"CHAT","message":"wrapped"}\n```'
    )

    assert parsed.message == "wrapped"


def test_schema_missing_field_raises_parse_error() -> None:
    with pytest.raises(ParseError, match="message"):
        OutputParser().parse('{"intent":"CHAT"}')


@pytest.mark.asyncio
async def test_repair_and_retry_succeeds_on_first_retry() -> None:
    attempts: list[int] = []

    async def repair(raw_output: str, error: ParseError, attempt: int) -> str:
        attempts.append(attempt)
        return '{"intent":"CHAT","message":"fixed"}'

    parsed = await OutputParser(repair_fn=repair).repair_and_retry(
        "not json",
        ParseError("invalid JSON"),
    )

    assert attempts == [1]
    assert parsed.intent is IntentType.CHAT
    assert parsed.message == "fixed"


@pytest.mark.asyncio
async def test_repair_and_retry_degrades_after_max_retry() -> None:
    async def repair(raw_output: str, error: ParseError, attempt: int) -> str:
        return '{"intent":"CHAT"}'

    parsed = await OutputParser(repair_fn=repair, max_retries=2).repair_and_retry(
        "bad",
        ParseError("invalid JSON"),
    )

    assert parsed.intent is IntentType.CANNOT_COMPLETE
    assert "message is required" in parsed.reason
