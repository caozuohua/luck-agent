from __future__ import annotations

import json
import sqlite3

import pytest

from runtime.evaluation import evaluate_call
from runtime.history_audit import audit, recovery_recommendation, summarize_trace


def checkpoint(ts, observations, *, decision=None, step=1, call=None):
    return {"ts": f"2026-09-01T00:00:{ts:02d}+00:00",
            "updated_channels": ["branch:to:planner"] if decision else ["branch:to:supervisor"],
            "channel_values": {"step_count": step, "decision": decision,
                               "last_parsed": {"tool_call": call or {"name": "shell", "args": {}}},
                               "scratchpad": [{"role": "observation", "content": json.dumps(o)} for o in observations]}}


ERROR = {"status": "error", "error": "command is required", "metadata": {"tool_name": "shell"}}
OK = {"status": "ok", "metadata": {"tool_name": "shell"}}


def test_checkpoint_copies_are_not_new_calls_but_identical_retries_are():
    trace = summarize_trace([checkpoint(0, [ERROR]), checkpoint(1, [ERROR], decision="retry"),
                             checkpoint(2, [ERROR, ERROR], step=2),
                             checkpoint(3, [ERROR, ERROR], step=2, decision="fail")])
    assert len(trace["calls"]) == 2
    assert trace["identical_call_repeats_after_error"] == 1
    assert trace["adjacent_identical_call_recoveries"] == 0
    assert trace["calls"][0]["supervisor_at"].endswith("01+00:00")


def test_different_successful_call_is_not_recovery_of_failed_call():
    trace = summarize_trace([checkpoint(0, [ERROR]),
                             checkpoint(1, [ERROR, OK], step=2, call={"name": "shell", "args": {"command": "pwd"}}),
                             checkpoint(2, [ERROR, OK], decision="done", step=3)])
    assert trace["done_after_tool_error"]
    assert trace["adjacent_identical_call_recoveries"] == 0


@pytest.mark.parametrize("category,kwargs,expected", [
    ("permission", {}, "escalate"),
    ("configuration", {}, "escalate"),
    ("approval", {}, "escalate"),
    ("invalid_arguments", {}, "repair_or_escalate"),
    ("invalid_arguments", {"arguments_repaired": True}, "retry_repaired_call"),
    ("invalid_arguments", {"arguments_repaired": True, "retries": 1}, "repair_or_escalate"),
    ("timeout", {"mutating": True, "effect_known": False}, "verify_effect_then_escalate"),
    ("timeout", {"mutating": True, "idempotent": False}, "verify_effect_then_escalate"),
    ("transient", {"retries": 1}, "retry_with_backoff"),
    ("transient", {"retries": 2}, "escalate"),
    ("transient", {"elapsed_seconds": 120}, "escalate"),
    ("other", {}, "escalate"),
])
def test_fault_escalation_oracles(category, kwargs, expected):
    assert recovery_recommendation(category, **kwargs) == expected


def test_selection_schema_and_wrong_target_effect_are_separate():
    result = evaluate_call(selected_tool="shell", arguments={"timeout": "slow"},
                           allowed_tools=["service_health"],
                           schema={"type": "object", "required": ["command"],
                                   "properties": {"timeout": {"type": "number"}}},
                           expected_effect={"target": "gcp", "active": True},
                           observed_effect={"target": "aws", "active": True}, evidence_ref="readback:1")
    assert result["tool_selection_correct"] is False
    assert result["arguments_valid"] is False
    assert result["side_effect_correct"] is False
    assert {e["rule"] for e in result["argument_errors"]} == {"required", "type"}


def test_missing_evidence_is_unknown_not_success():
    result = evaluate_call(selected_tool="backup", arguments={}, expected_effect={"valid": True},
                           observed_effect={"valid": True})
    assert result["tool_selection_correct"] is None
    assert result["arguments_valid"] is None
    assert result["side_effect_correct"] is None


def test_audit_includes_wal_and_does_not_mutate_source(tmp_path):
    db_path, graph_path = tmp_path / "agent.db", tmp_path / "graph.db"
    db = sqlite3.connect(db_path)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE goals(id TEXT,user_id TEXT,status TEXT,created_at INTEGER,updated_at INTEGER,tool_calls TEXT,retry_count INTEGER);
        INSERT INTO goals VALUES('g1','u1','DONE',1,2,'[]',0);
        CREATE TABLE operation_audit(tool_name TEXT,decision TEXT,details TEXT);
        CREATE TABLE patterns(tool_name TEXT,pattern_type TEXT);
        CREATE TABLE context_summaries(id TEXT);
    """)
    graph = sqlite3.connect(graph_path)
    graph.execute("CREATE TABLE checkpoints(thread_id TEXT,type TEXT,checkpoint BLOB,checkpoint_ns TEXT,checkpoint_id TEXT)")
    graph.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", ("u1:g1", "json", json.dumps(checkpoint(0, [], decision="done")), "", "1"))
    graph.commit()
    before = list(db.iterdump())
    result = audit(str(db_path), str(graph_path))
    assert result["coverage"]["matched_goals"] == 1
    assert result["goals"]["status_done_rate"] == 1
    assert result["goals"]["verified_success_rate"] is None
    assert result["tools"]["calls"] == 0
    assert list(db.iterdump()) == before
    graph.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", ("u1:g1", "pickle", b"not decoded", "", "2"))
    graph.commit()
    damaged = audit(str(db_path), str(graph_path))
    assert damaged["coverage"]["invalid_traces"] == 1
    assert damaged["coverage"]["matched_goals"] == 0
    db.close()
    graph.close()


def test_missing_database_is_not_created(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        audit(str(tmp_path / "missing.db"), str(tmp_path / "also_missing.db"))
    assert not (tmp_path / "missing.db").exists()
