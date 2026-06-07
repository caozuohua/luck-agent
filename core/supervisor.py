"""
core/supervisor.py — Luck-Agent 2.0 Supervisor.

The Supervisor validates StepResult outputs and makes a normalized decision:
- pass: continue to the next step
- retry: rerun current step if retry budget allows
- block: stop and wait for human/system intervention
- fail: terminal failure

It also captures low-friction lesson candidates into Memory.lessons so repeated
errors can be retrieved before future executions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from core.log import get_logger
from core.protocols import VERIFICATION_SCHEMA, VerificationResult, validate_json

log = get_logger()

Decision = Literal["pass", "retry", "block", "fail"]


@dataclass
class SupervisorDecision:
    decision: Decision
    verification: dict[str, Any]
    reason: str = ""
    next_action: str = ""
    lesson_id: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Supervisor:
    """Result verifier and lesson-capture layer for ExecutionEngine."""

    def __init__(self, memory=None, default_confidence: float = 0.55) -> None:
        self.memory = memory
        self.default_confidence = default_confidence

    def review_step_result(
        self,
        *,
        goal: dict,
        step: dict,
        result,
        retry_count: int = 0,
        max_retry: int = 0,
    ) -> SupervisorDecision:
        """Review a StepResult-like object and return a normalized decision."""
        ok = bool(getattr(result, "ok", False))
        error = str(getattr(result, "error", "") or "")
        hint = str(getattr(result, "hint", "") or "")
        blocking = bool(getattr(result, "blocking", False))
        action = str(getattr(result, "action", "") or step.get("name", ""))

        if ok:
            verification = VerificationResult(
                verdict="passed",
                is_goal_complete=False,
                reason="step result ok",
                next_action="continue",
                blocking=False,
                confidence=0.9,
            ).to_dict()
            self._validate_verification(verification)
            return SupervisorDecision(
                decision="pass",
                verification=verification,
                reason="step passed",
                next_action="continue",
            )

        lesson_id = self._save_lesson_candidate(
            goal=goal,
            step=step,
            action=action,
            error=error,
            hint=hint,
        )

        if not blocking and retry_count < max_retry:
            verification = VerificationResult(
                verdict="failed",
                is_goal_complete=False,
                reason=error or "step failed",
                next_action="retry",
                blocking=False,
                confidence=0.75,
            ).to_dict()
            self._validate_verification(verification)
            return SupervisorDecision(
                decision="retry",
                verification=verification,
                reason=error or "step failed, retry allowed",
                next_action="retry",
                lesson_id=lesson_id,
                meta={"retry_count": retry_count, "max_retry": max_retry},
            )

        verification = VerificationResult(
            verdict="blocked" if blocking else "failed",
            is_goal_complete=False,
            reason=error or "step failed",
            next_action="human_or_controller_repair",
            blocking=True,
            confidence=0.85,
        ).to_dict()
        self._validate_verification(verification)
        return SupervisorDecision(
            decision="block",
            verification=verification,
            reason=error or "step blocked",
            next_action="human_or_controller_repair",
            lesson_id=lesson_id,
            meta={"retry_count": retry_count, "max_retry": max_retry},
        )

    def review_goal_completion(self, *, goal: dict, steps: list[dict], complete: bool) -> dict[str, Any]:
        """Normalize goal-completion verification."""
        verification = VerificationResult(
            verdict="passed" if complete else "continue",
            is_goal_complete=complete,
            reason="goal complete" if complete else "goal not complete",
            next_action="finish" if complete else "continue",
            blocking=False,
            confidence=0.9 if complete else 0.65,
        ).to_dict()
        self._validate_verification(verification)
        return verification

    def _save_lesson_candidate(
        self,
        *,
        goal: dict,
        step: dict,
        action: str,
        error: str,
        hint: str,
    ) -> int | None:
        if not self.memory or not error:
            return None
        domain = self._domain_from_intent(goal.get("intent", "general"))
        task_type = goal.get("intent", "general")
        error_pattern = self._normalize_error(error)
        if not error_pattern:
            return None
        lesson = {
            "domain": domain,
            "task_type": task_type,
            "error_pattern": error_pattern,
            "root_cause": error[:240],
            "solution": hint or "Review step output and add a controller-specific repair path.",
            "prevention": f"Before action `{action}`, check prior lessons and preconditions.",
            "confidence": self.default_confidence,
        }
        try:
            lesson_id = self.memory.save_lesson(lesson)
            log.info("lesson_candidate_saved", lesson_id=lesson_id, domain=domain, task_type=task_type)
            return lesson_id
        except Exception as e:
            log.warning("lesson_candidate_save_failed", error=str(e), action=action)
            return None

    @staticmethod
    def _validate_verification(payload: dict[str, Any]) -> None:
        ok, err = validate_json(payload, VERIFICATION_SCHEMA)
        if not ok:
            raise ValueError(f"invalid verification payload: {err}")

    @staticmethod
    def _domain_from_intent(intent: str) -> str:
        if intent.startswith("blog"):
            return "blog"
        if intent.startswith("github") or intent in {"git_push", "github_code"}:
            return "github"
        if intent.startswith("shell"):
            return "shell"
        if intent.startswith("pkb"):
            return "pkb"
        return "general"

    @staticmethod
    def _normalize_error(error: str, limit: int = 160) -> str:
        text = " ".join((error or "").strip().split())
        return text[:limit]
