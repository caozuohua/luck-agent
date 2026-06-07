"""
controllers/base.py — Standard controller interface for Luck-Agent 2.0.

Controllers implement domain-specific execution while ExecutionEngine remains
intent-agnostic. A controller defines a deterministic plan, executes one step,
and decides whether the goal is complete.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.execution_engine import StepResult, StepSpec


class BaseController(ABC):
    """Base class for intent/domain controllers."""

    intent: str = "general"

    @abstractmethod
    async def build_plan(self, goal: dict) -> list[StepSpec]:
        """Build a deterministic step plan for a goal."""
        raise NotImplementedError

    @abstractmethod
    async def execute_step(self, goal: dict, step: StepSpec) -> StepResult:
        """Execute one step and return a normalized StepResult."""
        raise NotImplementedError

    @abstractmethod
    async def is_goal_complete(self, goal: dict, steps: list[dict]) -> bool:
        """Return whether the goal meets completion criteria."""
        raise NotImplementedError

    @staticmethod
    def all_required_steps_done(steps: list[dict]) -> bool:
        """Default completion helper based on persisted step status."""
        if not steps:
            return False
        for step in steps:
            raw_input = step.get("input") or {}
            required = bool(raw_input.get("required", True))
            if required and step.get("status") != "done":
                return False
        return True
