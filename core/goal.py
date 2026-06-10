"""
core/goal.py — Luck-Agent 2.0 GoalManager.

This module turns user requests into persistent long-running goals and provides
safe lifecycle primitives: create, start, pause, resume, cancel, block, fail,
complete, and recover interrupted goals.

It intentionally does not execute tools. ExecutionEngine and domain controllers
will consume the GoalManager API in later PRs.
"""
from __future__ import annotations

import time
from typing import Any

from core.log import get_logger
from core.protocols import (
    GOAL_SCHEMA,
    STEP_SCHEMA,
    Goal,
    GoalStep,
    new_id,
    validate_json,
)

log = get_logger()


ACTIVE_STATUSES = {"pending", "running", "interrupted"}
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
PAUSABLE_STATUSES = {"pending", "running", "interrupted", "blocked"}
RESUMABLE_STATUSES = {"pending", "blocked", "interrupted"}


DEFAULT_SUCCESS_CRITERIA: dict[str, list[str]] = {
    "blog_write": [
        "内容已生成或更新",
        "目标文件已写入",
        "本地构建或基础检查通过",
        "变更已提交并推送",
        "发布结果已验证或明确给出阻塞原因",
    ],
    "github_code": [
        "目标文件已读取或修改",
        "变更内容已持久化",
        "操作结果已验证",
    ],
    "shell_run": [
        "命令已执行",
        "返回码和关键输出已记录",
        "失败时已记录错误和后续建议",
    ],
    "general": [
        "任务目标已明确",
        "必要步骤已记录",
        "完成、失败或阻塞状态已明确",
    ],
}


class GoalError(RuntimeError):
    """Goal lifecycle error."""


class GoalManager:
    """Persistent lifecycle manager for long-running goals."""

    def __init__(self, memory) -> None:
        self.memory = memory

    def create_goal(
        self,
        *,
        user_id: str,
        chat_id: str,
        title: str,
        intent: str = "general",
        success_criteria: list[str] | None = None,
        plan: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        status: str = "pending",
    ) -> str:
        goal = Goal(
            goal_id=new_id("goal"),
            user_id=user_id,
            chat_id=chat_id,
            title=title.strip() or "未命名目标",
            intent=intent.strip() or "general",
            status=status,  # type: ignore[arg-type]
            success_criteria=success_criteria or self.default_success_criteria(intent),
            plan=plan or {},
            artifacts=artifacts or [],
        )
        payload = goal.to_dict()
        ok, err = validate_json(payload, GOAL_SCHEMA)
        if not ok:
            raise GoalError(f"invalid goal payload: {err}")
        self.memory.create_goal(payload)
        log.info("goal_created", goal_id=goal.goal_id, intent=goal.intent, user_id=user_id[:8])
        return goal.goal_id

    def create_goal_from_message(
        self,
        *,
        user_id: str,
        chat_id: str,
        text: str,
        intent: str = "general",
        success_criteria: list[str] | None = None,
        plan: dict[str, Any] | None = None,
    ) -> str:
        title = self._title_from_message(text)
        return self.create_goal(
            user_id=user_id,
            chat_id=chat_id,
            title=title,
            intent=intent,
            success_criteria=success_criteria,
            plan=plan or {"source_message": text},
        )

    def get_goal(self, goal_id: str) -> dict:
        goal = self.memory.get_goal(goal_id)
        if not goal:
            raise GoalError(f"goal not found: {goal_id}")
        return goal

    def list_goals(
        self,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        return self.memory.list_goals(
            user_id=user_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    def list_active_goals(self, user_id: str | None = None, limit: int = 20) -> list[dict]:
        goals: list[dict] = []
        for status in ACTIVE_STATUSES:
            goals.extend(self.memory.list_goals(user_id=user_id, status=status, limit=limit))
        return sorted(goals, key=lambda g: g.get("updated_at", 0), reverse=True)[:limit]

    def start_goal(self, goal_id: str, current_step: str | None = None) -> dict:
        goal = self.get_goal(goal_id)
        self._ensure_not_terminal(goal)
        updates: dict[str, Any] = {"status": "running"}
        if current_step is not None:
            updates["current_step"] = current_step
        self.memory.update_goal(goal_id, **updates)
        log.info("goal_started", goal_id=goal_id, current_step=current_step or goal.get("current_step", ""))
        return self.get_goal(goal_id)

    def pause_goal(self, goal_id: str, reason: str = "") -> dict:
        goal = self.get_goal(goal_id)
        if goal["status"] not in PAUSABLE_STATUSES:
            raise GoalError(f"goal status cannot be paused: {goal['status']}")
        updated = self.memory.update_goal_if_status(
            goal_id,
            PAUSABLE_STATUSES,
            status="interrupted",
            error=reason,
        )
        if not updated:
            current = self.get_goal(goal_id)
            if current["status"] in TERMINAL_STATUSES:
                return current
            raise GoalError(f"goal status cannot be paused: {current['status']}")
        log.info("goal_paused", goal_id=goal_id, reason=reason[:120])
        return self.get_goal(goal_id)

    def resume_goal(self, goal_id: str) -> dict:
        goal = self.get_goal(goal_id)
        if goal["status"] not in RESUMABLE_STATUSES:
            raise GoalError(f"goal status cannot be resumed: {goal['status']}")
        self.memory.update_goal(goal_id, status="running", error="")
        log.info("goal_resumed", goal_id=goal_id, current_step=goal.get("current_step", ""))
        return self.get_goal(goal_id)

    def cancel_goal(self, goal_id: str, reason: str = "user_cancelled") -> dict:
        goal = self.get_goal(goal_id)
        if goal["status"] in TERMINAL_STATUSES:
            return goal
        self.memory.update_goal(goal_id, status="cancelled", error=reason)
        log.info("goal_cancelled", goal_id=goal_id, reason=reason[:120])
        return self.get_goal(goal_id)

    def block_goal(self, goal_id: str, reason: str, current_step: str | None = None) -> dict:
        self.get_goal(goal_id)
        updates: dict[str, Any] = {"status": "blocked", "error": reason}
        if current_step is not None:
            updates["current_step"] = current_step
        self.memory.update_goal(goal_id, **updates)
        log.warning("goal_blocked", goal_id=goal_id, reason=reason[:160])
        return self.get_goal(goal_id)

    def fail_goal(self, goal_id: str, error: str) -> dict:
        self.get_goal(goal_id)
        self.memory.update_goal(goal_id, status="failed", error=error)
        log.error("goal_failed", goal_id=goal_id, error=error[:200])
        return self.get_goal(goal_id)

    def complete_goal(self, goal_id: str, artifacts: list[dict[str, Any]] | None = None) -> dict:
        goal = self.get_goal(goal_id)
        merged_artifacts = list(goal.get("artifacts") or [])
        if artifacts:
            merged_artifacts.extend(artifacts)
        self.memory.update_goal(
            goal_id,
            status="done",
            error="",
            artifacts=merged_artifacts,
        )
        log.info("goal_done", goal_id=goal_id, artifacts=len(merged_artifacts))
        return self.get_goal(goal_id)

    def set_current_step(self, goal_id: str, step_name: str) -> dict:
        self.get_goal(goal_id)
        self.memory.update_goal(goal_id, current_step=step_name)
        return self.get_goal(goal_id)

    def append_artifact(self, goal_id: str, artifact: dict[str, Any]) -> dict:
        goal = self.get_goal(goal_id)
        artifacts = list(goal.get("artifacts") or [])
        artifacts.append(artifact)
        self.memory.update_goal(goal_id, artifacts=artifacts)
        return self.get_goal(goal_id)

    def create_step(
        self,
        *,
        goal_id: str,
        name: str,
        input: dict[str, Any] | None = None,
        status: str = "pending",
    ) -> str:
        self.get_goal(goal_id)
        step = GoalStep(
            step_id=new_id("step"),
            goal_id=goal_id,
            name=name,
            status=status,  # type: ignore[arg-type]
            input=input or {},
        )
        payload = step.to_dict()
        ok, err = validate_json(payload, STEP_SCHEMA)
        if not ok:
            raise GoalError(f"invalid step payload: {err}")
        self.memory.create_goal_step(payload)
        log.info("goal_step_created", goal_id=goal_id, step_id=step.step_id, name=name)
        return step.step_id

    def start_step(self, step_id: str) -> dict:
        step = self.memory.get_goal_step(step_id)
        if not step:
            raise GoalError(f"step not found: {step_id}")
        self.memory.update_goal_step(step_id, status="running", started_at=time.time())
        self.memory.update_goal(step["goal_id"], current_step=step["name"], status="running")
        return self.memory.get_goal_step(step_id)

    def finish_step(self, step_id: str, output: dict[str, Any] | None = None) -> dict:
        step = self.memory.get_goal_step(step_id)
        if not step:
            raise GoalError(f"step not found: {step_id}")
        self.memory.update_goal_step(
            step_id,
            status="done",
            output=output or {},
            error="",
            finished_at=time.time(),
        )
        return self.memory.get_goal_step(step_id)

    def fail_step(self, step_id: str, error: str, output: dict[str, Any] | None = None) -> dict:
        step = self.memory.get_goal_step(step_id)
        if not step:
            raise GoalError(f"step not found: {step_id}")
        self.memory.update_goal_step(
            step_id,
            status="failed",
            output=output or {},
            error=error,
            finished_at=time.time(),
        )
        self.memory.update_goal(step["goal_id"], status="blocked", error=error)
        return self.memory.get_goal_step(step_id)

    def get_steps(self, goal_id: str) -> list[dict]:
        self.get_goal(goal_id)
        return self.memory.get_goal_steps(goal_id)

    def progress(self, goal_id: str) -> dict:
        """Return computed progress for cards, commands, and future runtime decisions."""
        goal = self.get_goal(goal_id)
        steps = self.memory.get_goal_steps(goal_id)
        total = len(steps)
        done = sum(1 for s in steps if s.get("status") == "done")
        failed = sum(1 for s in steps if s.get("status") == "failed")
        running = sum(1 for s in steps if s.get("status") == "running")
        blocked = goal.get("status") == "blocked" or any(s.get("status") == "blocked" for s in steps)
        percent = 100 if goal.get("status") == "done" else int(done * 100 / total) if total else 0
        return {
            "goal_id": goal_id,
            "title": goal.get("title", ""),
            "intent": goal.get("intent", "general"),
            "status": goal.get("status", ""),
            "current_step": goal.get("current_step", ""),
            "total_steps": total,
            "done_steps": done,
            "failed_steps": failed,
            "running_steps": running,
            "blocked": blocked,
            "percent": percent,
            "error": goal.get("error", ""),
            "updated_at": goal.get("updated_at"),
        }

    def summary(self, goal_id: str) -> str:
        """Return a concise human-readable goal summary for Lark cards or commands."""
        p = self.progress(goal_id)
        bits = [
            f"目标：{p['title']}",
            f"状态：{p['status']}",
            f"进度：{p['done_steps']}/{p['total_steps']} ({p['percent']}%)",
        ]
        if p.get("current_step"):
            bits.append(f"当前步骤：{p['current_step']}")
        if p.get("error"):
            bits.append(f"问题：{p['error']}")
        return "\n".join(bits)

    def timeline(self, goal_id: str) -> list[dict]:
        """Return ordered timeline records derived from goal and goal_steps."""
        goal = self.get_goal(goal_id)
        rows: list[dict] = [
            {
                "type": "goal_created",
                "goal_id": goal_id,
                "title": goal.get("title", ""),
                "status": goal.get("status", ""),
                "at": goal.get("created_at"),
            }
        ]
        for step in self.memory.get_goal_steps(goal_id):
            rows.append({
                "type": "step",
                "goal_id": goal_id,
                "step_id": step.get("step_id"),
                "name": step.get("name"),
                "status": step.get("status"),
                "error": step.get("error", ""),
                "started_at": step.get("started_at"),
                "finished_at": step.get("finished_at"),
                "at": step.get("started_at") or step.get("created_at"),
            })
        rows.append({
            "type": "goal_updated",
            "goal_id": goal_id,
            "status": goal.get("status", ""),
            "current_step": goal.get("current_step", ""),
            "error": goal.get("error", ""),
            "at": goal.get("updated_at"),
        })
        return sorted(rows, key=lambda item: item.get("at") or 0)

    def resume_all_recoverable(self, user_id: str | None = None) -> list[dict]:
        """Resume all interrupted/blocked/pending goals for a user or globally."""
        resumed: list[dict] = []
        for status in RESUMABLE_STATUSES:
            for goal in self.memory.list_goals(user_id=user_id, status=status, limit=100):
                try:
                    resumed.append(self.resume_goal(goal["goal_id"]))
                except GoalError as e:
                    log.warning("goal_resume_skipped", goal_id=goal.get("goal_id"), error=str(e))
        return resumed

    def recover_interrupted_goals(self, stale_after_seconds: int = 300) -> list[dict]:
        """Mark stale running goals as interrupted and return recoverable goals."""
        now = time.time()
        running_goals = self._list_all_goals(status="running")
        pending_goals = self._list_all_goals(status="pending")
        interrupted_goals = self._list_all_goals(status="interrupted")

        recoverable = {
            goal["goal_id"]: goal
            for goal in pending_goals + interrupted_goals
        }
        for goal in running_goals:
            updated_at = float(goal.get("updated_at") or 0)
            if now - updated_at >= stale_after_seconds:
                self.memory.update_goal(
                    goal["goal_id"],
                    status="interrupted",
                    error=f"stale running goal after {stale_after_seconds}s",
                )
                recoverable[goal["goal_id"]] = self.get_goal(goal["goal_id"])
        return list(recoverable.values())

    def _list_all_goals(self, *, status: str, page_size: int = 100) -> list[dict]:
        goals: list[dict] = []
        offset = 0
        while True:
            page = self.memory.list_goals(
                status=status,
                limit=page_size,
                offset=offset,
            )
            goals.extend(page)
            if len(page) < page_size:
                return goals
            offset += len(page)

    @staticmethod
    def default_success_criteria(intent: str) -> list[str]:
        return list(DEFAULT_SUCCESS_CRITERIA.get(intent, DEFAULT_SUCCESS_CRITERIA["general"]))

    @staticmethod
    def _title_from_message(text: str, limit: int = 60) -> str:
        title = " ".join((text or "").strip().split())
        if not title:
            return "未命名目标"
        if len(title) <= limit:
            return title
        return title[:limit].rstrip() + "…"

    @staticmethod
    def _ensure_not_terminal(goal: dict) -> None:
        if goal.get("status") in TERMINAL_STATUSES:
            raise GoalError(f"goal already terminal: {goal.get('status')}")
