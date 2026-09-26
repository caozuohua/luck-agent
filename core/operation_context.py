"""Per-invocation correlation, isolated across concurrent graph tasks."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from typing import Iterator


def request_reference(source: str) -> str:
    """Stable correlation without persisting card confirmation tokens in IDs."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest() if source else ""


@dataclass(frozen=True)
class OperationContext:
    request_id: str = ""
    goal_id: str = ""
    run_id: str = ""
    step_id: str = ""
    chat_id: str = ""
    project_id: str = ""
    operation_id: str = ""
    parent_attempt_id: str = ""


current_operation: ContextVar[OperationContext] = ContextVar(
    "current_operation", default=OperationContext(),
)


@contextmanager
def operation_context(context: OperationContext) -> Iterator[None]:
    token = current_operation.set(context)
    try:
        yield
    finally:
        current_operation.reset(token)
