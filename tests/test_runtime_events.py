from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core.memory import Memory
from runtime.events import NoopRuntimeEventRecorder, RuntimeEventRecorder


class RuntimeEventsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "memory.db")
        self.memory = Memory(self.db_path)

    def tearDown(self) -> None:
        conn = getattr(self.memory._local, "conn", None)
        if conn is not None:
            conn.close()
            self.memory._local.conn = None
        self.temp_dir.cleanup()

    def test_schema_is_created_with_required_indexes(self) -> None:
        with self.memory._conn() as conn:
            columns = conn.execute("PRAGMA table_info(runtime_events)").fetchall()
            index_list = conn.execute("PRAGMA index_list(runtime_events)").fetchall()
            indexes = conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='runtime_events'"
            ).fetchall()

        self.assertEqual(
            [column["name"] for column in columns],
            [
                "id",
                "event_id",
                "goal_id",
                "step_id",
                "skill",
                "intent",
                "event_type",
                "status",
                "user_id",
                "chat_id",
                "payload",
                "created_at",
            ],
        )
        index_sql = {row["name"]: row["sql"] for row in indexes}
        unique_indexes = {row["name"] for row in index_list if row["unique"]}
        self.assertIn("sqlite_autoindex_runtime_events_1", unique_indexes)
        self.assertIn(
            "ON runtime_events(goal_id, id)",
            index_sql["idx_runtime_events_goal"],
        )
        self.assertIn(
            "ON runtime_events(skill, id)",
            index_sql["idx_runtime_events_skill"],
        )
        self.assertIn(
            "ON runtime_events(event_type, id)",
            index_sql["idx_runtime_events_type"],
        )

    def test_record_and_list_events_in_id_order_with_decoded_payload(self) -> None:
        recorder = RuntimeEventRecorder(self.memory)
        recorder.record(
            "goal.created",
            goal_id="g1",
            skill="blog_write",
            payload={"n": 1},
            event_id="event_1",
            created_at=10.0,
        )
        recorder.record(
            "queue.submitted",
            goal_id="g1",
            skill="blog_write",
            payload={"n": 2},
            event_id="event_2",
            created_at=5.0,
        )

        events = self.memory.list_runtime_events(goal_id="g1")

        self.assertEqual(
            [event["event_type"] for event in events],
            ["goal.created", "queue.submitted"],
        )
        self.assertEqual(events[0]["payload"], {"n": 1})
        self.assertEqual(
            set(events[0]),
            {
                "id",
                "event_id",
                "goal_id",
                "step_id",
                "skill",
                "intent",
                "event_type",
                "status",
                "user_id",
                "chat_id",
                "payload",
                "created_at",
            },
        )

    def test_list_filters_by_goal_skill_and_event_type(self) -> None:
        events = [
            {
                "event_id": "event_1",
                "goal_id": "g1",
                "skill": "blog",
                "event_type": "goal.created",
            },
            {
                "event_id": "event_2",
                "goal_id": "g2",
                "skill": "blog",
                "event_type": "goal.created",
            },
            {
                "event_id": "event_3",
                "goal_id": "g1",
                "skill": "research",
                "event_type": "goal.completed",
            },
        ]
        for event in events:
            self.memory.append_runtime_event(event)

        result = self.memory.list_runtime_events(
            goal_id="g1",
            skill="research",
            event_type="goal.completed",
        )

        self.assertEqual([event["event_id"] for event in result], ["event_3"])

    def test_list_limit_is_clamped_to_one_and_one_thousand(self) -> None:
        for index in range(1005):
            self.memory.append_runtime_event(
                {"event_id": f"event_{index}", "event_type": "test"}
            )

        self.assertEqual(len(self.memory.list_runtime_events(limit=0)), 1)
        self.assertEqual(len(self.memory.list_runtime_events(limit=5000)), 1000)

    def test_list_after_id_pages_through_later_events(self) -> None:
        for index in range(7):
            self.memory.append_runtime_event(
                {"event_id": f"event_{index}", "event_type": "test"}
            )

        first = self.memory.list_runtime_events(limit=3)
        second = self.memory.list_runtime_events(after_id=first[-1]["id"], limit=3)
        third = self.memory.list_runtime_events(after_id=second[-1]["id"], limit=3)

        self.assertEqual(
            [event["event_id"] for event in first + second + third],
            [f"event_{index}" for index in range(7)],
        )

    def test_duplicate_event_id_preserves_sqlite_unique_error(self) -> None:
        event = {"event_id": "event_duplicate", "event_type": "goal.created"}
        self.memory.append_runtime_event(event)

        with self.assertRaises(sqlite3.IntegrityError):
            self.memory.append_runtime_event(event)

    def test_recorder_generates_id_time_and_reasonable_defaults(self) -> None:
        before = time.time()

        RuntimeEventRecorder(self.memory).record("goal.created")

        after = time.time()
        event = self.memory.list_runtime_events()[0]
        self.assertTrue(event["event_id"].startswith("event_"))
        self.assertLessEqual(before, event["created_at"])
        self.assertLessEqual(event["created_at"], after)
        for field in (
            "goal_id",
            "step_id",
            "skill",
            "intent",
            "status",
            "user_id",
            "chat_id",
        ):
            self.assertEqual(event[field], "")
        self.assertEqual(event["payload"], {})

    def test_recorder_recursively_truncates_strings_and_stringifies_objects(self) -> None:
        class Unknown:
            def __str__(self) -> str:
                return "unknown-value"

        payload = {
            "top": "abcdef",
            "nested": ["123456", {"value": "uvwxyz"}],
            "tuple": ("longer", Unknown()),
        }

        RuntimeEventRecorder(self.memory, max_string_length=4).record(
            "goal.created",
            payload=payload,
        )

        stored = self.memory.list_runtime_events()[0]["payload"]
        self.assertEqual(
            stored,
            {
                "top": "abcd",
                "nest": ["1234", {"valu": "uvwx"}],
                "tupl": ["long", "unkn"],
            },
        )

    def test_recorder_bounds_depth_and_dict_keys(self) -> None:
        payload = {"long-key": {"nested": {"too_deep": "value"}}}

        RuntimeEventRecorder(
            self.memory,
            max_string_length=4,
            max_depth=2,
        ).record("goal.created", payload=payload)

        stored = self.memory.list_runtime_events()[0]["payload"]
        self.assertEqual(
            stored,
            {"long": {"nest": {"too_": "<max-depth>"}}},
        )

    def test_recorder_marks_circular_references(self) -> None:
        circular = []
        circular.append(circular)

        RuntimeEventRecorder(self.memory).record("goal.created", payload=circular)

        stored = self.memory.list_runtime_events()[0]["payload"]
        self.assertEqual(stored, ["[CIRCULAR]"])

    def test_recorder_bounds_total_nodes(self) -> None:
        RuntimeEventRecorder(self.memory, max_nodes=5).record(
            "goal.created",
            payload=list(range(20)),
        )

        stored = self.memory.list_runtime_events()[0]["payload"]
        self.assertIn("<max-nodes>", repr(stored))

    def test_recorder_falls_back_to_bounded_summary_for_large_payload(self) -> None:
        RuntimeEventRecorder(
            self.memory,
            max_string_length=1000,
            max_payload_bytes=120,
        ).record("goal.created", payload={"large": "x" * 1000})

        with closing(sqlite3.connect(self.db_path)) as conn:
            raw_payload = conn.execute(
                "SELECT payload FROM runtime_events"
            ).fetchone()[0]
        stored = json.loads(raw_payload)

        self.assertLessEqual(len(raw_payload.encode("utf-8")), 120)
        self.assertTrue(stored["truncated"])
        self.assertIn("preview", stored)

    def test_recorder_honors_small_payload_byte_limit(self) -> None:
        RuntimeEventRecorder(
            self.memory,
            max_string_length=1000,
            max_payload_bytes=18,
        ).record("goal.created", payload={"large": "x" * 1000})

        with closing(sqlite3.connect(self.db_path)) as conn:
            raw_payload = conn.execute(
                "SELECT payload FROM runtime_events"
            ).fetchone()[0]

        self.assertLessEqual(len(raw_payload.encode("utf-8")), 18)
        self.assertEqual(json.loads(raw_payload), {"truncated": True})

    def test_recorder_redacts_before_payload_truncation(self) -> None:
        secret = "runtime-event-secret"
        RuntimeEventRecorder(
            self.memory,
            max_string_length=1000,
            max_payload_bytes=100,
        ).record(
            "goal.created",
            skill=f"token={secret}",
            payload={
                "token": secret,
                "text": (
                    f"access_key={secret} "
                    + ("x" * 1000)
                ),
            },
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT skill, payload FROM runtime_events"
            ).fetchone()

        self.assertNotIn(secret, repr(row))
        self.assertIn("[REDACTED]", repr(row))

    def test_memory_lists_latest_runtime_events_in_chronological_order(self) -> None:
        recorder = RuntimeEventRecorder(self.memory)
        for index in range(35):
            recorder.record(
                "step.reviewed",
                goal_id="goal-latest",
                step_id=f"step-{index + 1}",
                created_at=float(index + 1),
            )

        events = self.memory.list_latest_runtime_events(
            goal_id="goal-latest",
            limit=30,
        )

        self.assertEqual(len(events), 30)
        self.assertEqual(events[0]["step_id"], "step-6")
        self.assertEqual(events[-1]["step_id"], "step-35")

    def test_recorder_normalizes_non_finite_floats_to_strict_json(self) -> None:
        RuntimeEventRecorder(self.memory).record(
            "goal.created",
            payload={
                "nan": math.nan,
                "positive": math.inf,
                "negative": -math.inf,
            },
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            raw_payload = conn.execute(
                "SELECT payload FROM runtime_events"
            ).fetchone()[0]
        stored = json.loads(
            raw_payload,
            parse_constant=lambda value: self.fail(f"non-strict JSON: {value}"),
        )

        self.assertEqual(
            stored,
            {
                "nan": "NaN",
                "positive": "Infinity",
                "negative": "-Infinity",
            },
        )

    def test_recorder_returns_quickly_when_database_is_write_locked(self) -> None:
        locker = sqlite3.connect(self.db_path, timeout=1)
        locker.execute("BEGIN IMMEDIATE")
        try:
            with patch("runtime.events.log") as log:
                started = time.monotonic()
                RuntimeEventRecorder(self.memory).record(
                    "goal.created",
                    goal_id="g1",
                )
                elapsed = time.monotonic() - started
        finally:
            locker.rollback()
            locker.close()

        self.assertLess(elapsed, 0.5)
        log.error.assert_called_once_with(
            "runtime_event_write_failed",
            event_type="goal.created",
            goal_id="g1",
            error="OperationalError",
        )

    def test_recorder_logs_write_failure_and_does_not_raise(self) -> None:
        class FailingMemory:
            def append_runtime_event(self, event) -> None:
                raise sqlite3.OperationalError("database unavailable secret-token")

        recorder = RuntimeEventRecorder(FailingMemory())

        with patch("runtime.events.log") as log:
            recorder.record("goal.created", goal_id="g1")

        log.error.assert_called_once_with(
            "runtime_event_write_failed",
            event_type="goal.created",
            goal_id="g1",
            error="OperationalError",
        )
        self.assertNotIn("secret-token", repr(log.error.call_args))

    def test_recorder_persists_safe_marker_when_redaction_fails(self) -> None:
        class BrokenString:
            def __str__(self) -> str:
                raise ValueError("cannot stringify")

        with patch("runtime.events.log") as log:
            RuntimeEventRecorder(self.memory).record(
                "goal.created",
                goal_id="g1",
                payload={"broken": BrokenString()},
            )

        events = self.memory.list_runtime_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["payload"],
            {"broken": "[REDACTION_FAILED]"},
        )
        log.error.assert_not_called()

    def test_noop_recorder_accepts_the_full_signature(self) -> None:
        NoopRuntimeEventRecorder().record(
            "goal.created",
            goal_id="g1",
            step_id="s1",
            skill="blog",
            intent="write",
            status="running",
            user_id="u1",
            chat_id="c1",
            payload={"value": 1},
            event_id="event_1",
            created_at=1.0,
        )


if __name__ == "__main__":
    unittest.main()
