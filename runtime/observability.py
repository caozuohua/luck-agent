from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from core.goal import GoalError
from core.redaction import redact_text, redact_value


class RuntimeObservability:
    """Build bounded, read-only Runtime diagnostics for operator commands."""

    def __init__(
        self,
        *,
        goal_manager,
        runtime_manager,
        worker_manager,
        memory,
        overview_limit: int = 100,
        timeline_limit: int = 30,
    ) -> None:
        self.goal_manager = goal_manager
        self.runtime_manager = runtime_manager
        self.worker_manager = worker_manager
        self.memory = memory
        self.overview_limit = max(1, min(int(overview_limit), 500))
        self.timeline_limit = max(1, min(int(timeline_limit), 100))

    async def overview(self) -> str:
        worker_health = await self.worker_manager.health()
        queue = await self.runtime_manager.queue_snapshot()
        goals = self.goal_manager.list_goals(limit=self.overview_limit)
        statuses = Counter(str(goal.get("status") or "unknown") for goal in goals)
        recoverable = self._recoverable_count()
        events = self._latest_events(limit=1)

        lines = ["Runtime 状态"]
        lines.append(f"Worker：{int(worker_health.get('worker_count') or 0)}")
        for worker in worker_health.get("workers") or []:
            current = str(worker.get("current_goal_id") or "-")
            lines.append(
                "- {worker_id} running={running} goal={goal} "
                "processed={processed} failed={failed}".format(
                    worker_id=redact_text(worker.get("worker_id") or "unknown"),
                    running=bool(worker.get("running")),
                    goal=redact_text(current),
                    processed=int(worker.get("processed") or 0),
                    failed=int(worker.get("failed") or 0),
                )
            )

        counts = queue.get("counts") or {}
        queue_bits = [
            f"{redact_text(status)}={int(count)}"
            for status, count in sorted(counts.items())
        ]
        lines.append("队列：" + (", ".join(queue_bits) if queue_bits else "empty"))
        capacity = getattr(
            getattr(self.runtime_manager, "queue", None),
            "max_active",
            "?",
        )
        lines.append(
            f"运行槽位：{int(counts.get('running') or 0)}/{capacity}"
        )
        lines.append(f"可恢复 Goal：{recoverable}")
        status_bits = [
            f"{redact_text(status)}={count}"
            for status, count in sorted(statuses.items())
        ]
        lines.append(
            f"最近 Goal（{len(goals)}）："
            + (", ".join(status_bits) if status_bits else "none")
        )
        if events:
            latest = events[-1]
            lines.append(
                "最新事件：#{id} {time} {event}".format(
                    id=int(latest.get("id") or 0),
                    time=self._timestamp(latest.get("created_at")),
                    event=redact_text(latest.get("event_type") or ""),
                )
            )
        else:
            lines.append("最新事件：none")
        return "\n".join(lines)[:4000]

    async def goal_timeline(self, goal_id: str) -> str:
        safe_goal_id = redact_text(goal_id)[:200]
        try:
            goal = self.goal_manager.get_goal(goal_id)
        except GoalError:
            return f"未找到 Runtime Goal：{safe_goal_id}"

        progress = self.goal_manager.progress(goal_id)
        plan = goal.get("plan") if isinstance(goal.get("plan"), dict) else {}
        skill = str(plan.get("skill") or "")
        events = self._latest_events(goal_id=goal_id, limit=self.timeline_limit)
        if not skill:
            skill = next(
                (
                    str(event.get("skill") or "")
                    for event in events
                    if event.get("skill")
                ),
                "",
            )

        lines = [
            f"Runtime Goal：{safe_goal_id}",
            f"Skill：{redact_text(skill or '-')[:120]}",
            f"Intent：{redact_text(goal.get('intent') or '-')[:120]}",
            f"状态：{redact_text(goal.get('status') or '-')[:120]}",
            f"标题：{redact_text(goal.get('title') or '-')[:300]}",
            (
                "进度：{done}/{total} ({percent}%)".format(
                    done=int(progress.get("done_steps") or 0),
                    total=int(progress.get("total_steps") or 0),
                    percent=int(progress.get("percent") or 0),
                )
            ),
            f"当前步骤：{redact_text(progress.get('current_step') or '-')[:200]}",
        ]
        error = redact_text(goal.get("error") or "")[:500]
        if error:
            lines.append(f"错误：{error}")
        lines.append(f"最近事件（{len(events)}）：")
        for event in events:
            payload = self._payload_summary(event.get("payload"))
            line = (
                "{time} #{id} {event} status={status} step={step}".format(
                    time=self._timestamp(event.get("created_at")),
                    id=int(event.get("id") or 0),
                    event=redact_text(event.get("event_type") or "-")[:120],
                    status=redact_text(event.get("status") or "-")[:80],
                    step=redact_text(event.get("step_id") or "-")[:120],
                )
            )
            if payload:
                line += f" payload={payload}"
            lines.append(line)
        return "\n".join(lines)[:8000]

    def _latest_events(
        self,
        *,
        goal_id: str | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        latest = getattr(self.memory, "list_latest_runtime_events", None)
        if callable(latest):
            return latest(goal_id=goal_id, limit=limit)
        events = self.memory.list_runtime_events(goal_id=goal_id, limit=1000)
        return events[-limit:]

    def _recoverable_count(self) -> int:
        total = 0
        page_size = 100
        for status in ("pending", "interrupted"):
            offset = 0
            while True:
                page = self.goal_manager.list_goals(
                    status=status,
                    limit=page_size,
                    offset=offset,
                )
                total += len(page)
                if len(page) < page_size:
                    break
                offset += page_size
        return total

    @staticmethod
    def _timestamp(value: object) -> str:
        try:
            return datetime.fromtimestamp(
                float(value),
                tz=timezone.utc,
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError, OSError):
            return "-"

    @classmethod
    def _payload_summary(cls, payload: object) -> str:
        cleaned = cls._omit_identifiers(redact_value(payload))
        if cleaned in ({}, [], None, ""):
            return ""
        try:
            text = json.dumps(
                cleaned,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        except Exception:
            text = "[REDACTION_FAILED]"
        return redact_text(text)[:240]

    @classmethod
    def _omit_identifiers(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                key: cls._omit_identifiers(nested)
                for key, nested in value.items()
                if re.sub(r"[^a-z0-9]", "", str(key).lower())
                not in {"userid", "chatid"}
            }
        if isinstance(value, list):
            return [cls._omit_identifiers(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._omit_identifiers(item) for item in value)
        return value
