from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError

from runtime.contracts import RuntimeHandleResult


class RuntimeHandleResultTests(unittest.TestCase):
    def test_accepted_result_supports_attributes_and_mapping_access(self) -> None:
        accepted = RuntimeHandleResult(
            handled=True,
            skill="blog_write",
            goal_id="goal-1",
            intent="blog_write",
            status="accepted",
            queue_status="pending",
            summary="pending",
            reason="matched",
        )

        self.assertIsInstance(accepted, Mapping)
        self.assertEqual(accepted.skill, "blog_write")
        self.assertEqual(accepted["goal_id"], "goal-1")
        self.assertEqual(len(accepted), 8)
        self.assertEqual(
            list(accepted),
            [
                "handled",
                "skill",
                "goal_id",
                "intent",
                "status",
                "queue_status",
                "summary",
                "reason",
            ],
        )
        self.assertEqual(dict(accepted), accepted.to_dict())

    def test_result_is_immutable_and_mapping_is_read_only(self) -> None:
        result = RuntimeHandleResult(
            handled=False,
            skill="legacy_react",
            goal_id="",
            intent="general",
            status="fallback",
            queue_status="",
            summary="",
            reason="legacy fallback",
        )

        with self.assertRaises(FrozenInstanceError):
            result.status = "accepted"
        with self.assertRaises(TypeError):
            result["status"] = "accepted"

    def test_handled_result_requires_runtime_identity(self) -> None:
        required_fields = ("skill", "goal_id", "intent")
        valid = {
            "handled": True,
            "skill": "blog_write",
            "goal_id": "goal-1",
            "intent": "blog_write",
            "status": "accepted",
            "queue_status": "pending",
            "summary": "pending",
            "reason": "matched",
        }

        for field in required_fields:
            with self.subTest(field=field):
                values = dict(valid)
                values[field] = ""
                with self.assertRaisesRegex(
                    ValueError,
                    "handled result requires skill, goal_id, and intent",
                ):
                    RuntimeHandleResult(**values)

    def test_fallback_result_rejects_goal_id(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "fallback result cannot include goal_id",
        ):
            RuntimeHandleResult(
                handled=False,
                skill="legacy_react",
                goal_id="goal-1",
                intent="general",
                status="fallback",
                queue_status="",
                summary="",
                reason="legacy fallback",
            )


if __name__ == "__main__":
    unittest.main()
