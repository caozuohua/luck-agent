from __future__ import annotations

import unittest
from unittest.mock import Mock

from core.goal import GoalError
from runtime.observability import RuntimeObservability


class FakeGoalManager:
    def __init__(self) -> None:
        self.goals = [
            {
                "goal_id": "goal-running",
                "title": "整理博客选题",
                "intent": "blog_write",
                "status": "running",
                "current_step": "generate_topics",
                "plan": {"skill": "blog_write"},
                "error": "",
                "updated_at": 200.0,
            },
            {
                "goal_id": "goal-pending",
                "title": "待处理",
                "intent": "blog_write",
                "status": "pending",
                "current_step": "",
                "plan": {"skill": "blog_write"},
                "error": "",
                "updated_at": 100.0,
            },
        ]

    def list_goals(
        self,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        goals = [
            goal
            for goal in self.goals
            if status is None or goal["status"] == status
        ]
        return goals[offset:offset + limit]

    def get_goal(self, goal_id: str) -> dict:
        for goal in self.goals:
            if goal["goal_id"] == goal_id:
                return goal
        raise GoalError(f"goal not found: {goal_id}")

    def progress(self, goal_id: str) -> dict:
        self.get_goal(goal_id)
        return {
            "done_steps": 1,
            "total_steps": 2,
            "percent": 50,
            "current_step": "generate_topics",
        }


class FakeRuntimeManager:
    queue = type("Queue", (), {"max_active": 2})()

    async def queue_snapshot(self) -> dict:
        return {
            "counts": {"pending": 1, "running": 1},
            "items": [{"goal_id": "goal-running"}],
        }


class FakeWorkerManager:
    async def health(self) -> dict:
        return {
            "worker_count": 1,
            "workers": [
                {
                    "worker_id": "worker-1",
                    "running": True,
                    "current_goal_id": "goal-running",
                    "processed": 3,
                    "failed": 1,
                }
            ],
        }


class FakeMemory:
    def list_runtime_events(self, **kwargs) -> list[dict]:
        goal_id = kwargs.get("goal_id")
        events = [
            {
                "id": index,
                "goal_id": "goal-running",
                "step_id": f"step-{index}",
                "event_type": "step.reviewed",
                "status": "done",
                "payload": {
                    "token": "event-secret",
                    "user_id": "ou-private",
                    "value": index,
                },
                "created_at": float(index),
            }
            for index in range(1, 36)
        ]
        if goal_id and goal_id != "goal-running":
            return []
        return events[: kwargs.get("limit", 200)]


class RuntimeObservabilityTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self) -> RuntimeObservability:
        return RuntimeObservability(
            goal_manager=FakeGoalManager(),
            runtime_manager=FakeRuntimeManager(),
            worker_manager=FakeWorkerManager(),
            memory=FakeMemory(),
        )

    async def test_overview_reports_workers_queue_goals_and_latest_event(self) -> None:
        text = await self.make_service().overview()

        self.assertIn("worker-1", text)
        self.assertIn("goal-running", text)
        self.assertIn("pending=1", text)
        self.assertIn("running=1", text)
        self.assertIn("运行槽位：1/2", text)
        self.assertIn("可恢复 Goal：1", text)
        self.assertIn("最新事件：#35", text)
        self.assertLessEqual(len(text), 4000)

    async def test_goal_timeline_keeps_latest_30_events_and_redacts_payload(self) -> None:
        text = await self.make_service().goal_timeline("goal-running")

        self.assertIn("Skill：blog_write", text)
        self.assertIn("进度：1/2 (50%)", text)
        self.assertNotIn("step-1 ", text)
        self.assertNotIn("step-5 ", text)
        self.assertIn("step-6", text)
        self.assertIn("step-35", text)
        self.assertNotIn("event-secret", text)
        self.assertNotIn("ou-private", text)
        self.assertNotIn("user_id", text)
        self.assertLessEqual(len(text), 8000)

    async def test_missing_goal_returns_clear_message(self) -> None:
        text = await self.make_service().goal_timeline("goal-missing")

        self.assertEqual(text, "未找到 Runtime Goal：goal-missing")

    async def test_goal_query_failure_is_not_reported_as_missing(self) -> None:
        service = self.make_service()
        service.goal_manager.get_goal = Mock(
            side_effect=RuntimeError("database unavailable"),
        )

        with self.assertRaises(RuntimeError):
            await service.goal_timeline("goal-running")


if __name__ == "__main__":
    unittest.main()
