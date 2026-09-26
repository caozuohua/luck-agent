"""Read-only historical audit. Run with the deployment's Python environment.

python -m runtime.history_audit --db data/agent.db --graph data/graph_state.db
Never replays tools, imports checkpoint objects, or prints arguments/user text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


def distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median": median(ordered) if ordered else None,
        "p95": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)] if ordered else None,
        "max": max(ordered) if ordered else None,
    }


def error_class(error: str) -> str:
    lowered = error.lower()
    for marker, category in (
        ("permission_denied", "permission"),
        ("approval_required", "approval"),
        ("api_key", "configuration"),
        ("required", "invalid_arguments"),
        ("tool_not_found", "unknown_tool"),
        ("timeout", "timeout"),
    ):
        if marker in lowered:
            return category
    return "other"


def recovery_recommendation(
    category: str, *, retries: int = 0, elapsed_seconds: float = 0,
    mutating: bool = False, effect_known: bool = True,
    idempotent: bool = False, arguments_repaired: bool = False,
) -> str:
    """Shadow evaluation policy; does not change the production supervisor.

    Two retries / 120 seconds are initial review thresholds, not measured SLOs.
    """
    if category in {"permission", "approval", "configuration"}:
        return "escalate"
    if mutating and (not effect_known or not idempotent):
        return "verify_effect_then_escalate"
    if retries >= 2 or elapsed_seconds >= 120:
        return "escalate"
    if category in {"invalid_arguments", "unknown_tool"}:
        return "retry_repaired_call" if arguments_repaired and retries == 0 else "repair_or_escalate"
    if category in {"timeout", "transient"}:
        return "retry_with_backoff"
    return "escalate"


def read_snapshot(path: str) -> sqlite3.Connection:
    # SQLite backup includes committed WAL records and gives a consistent copy.
    source = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    copy = sqlite3.connect(":memory:")
    try:
        source.backup(copy)
    finally:
        source.close()
    copy.row_factory = sqlite3.Row
    return copy


def decode_checkpoint(kind: str, blob: bytes) -> dict:
    if kind == "json":
        return json.loads(blob)
    if kind == "msgpack":
        import ormsgpack

        # Deliberately no JsonPlusSerializer object construction or pickle.
        return ormsgpack.unpackb(blob)
    raise ValueError("unsupported checkpoint encoding")


def seconds(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def summarize_trace(checkpoints: list[dict]) -> dict[str, Any]:
    calls: list[dict] = []
    decisions: list[dict] = []
    seen_observations = 0
    final: dict = {}
    for checkpoint in checkpoints:
        state = checkpoint.get("channel_values", {})
        if not state:
            continue
        final = state
        observed = [x for x in state.get("scratchpad", []) if x.get("role") == "observation"]
        if len(observed) < seen_observations:
            raise ValueError("trace reset; split invocations before evaluation")
        # Same tool result is copied into subsequent checkpoints. Count each
        # scratchpad position only once, including identical repeated failures.
        for observation in observed[seen_observations:]:
            result = json.loads(observation["content"])
            call = (state.get("last_parsed") or {}).get("tool_call") or {}
            calls.append({
                "tool": result.get("metadata", {}).get("tool_name") or call.get("name", "unknown"),
                "requested_tool": call.get("name", "unknown"),
                "arg_fingerprint": hashlib.sha256(json.dumps(call.get("args", {}), sort_keys=True).encode()).hexdigest(),
                "arg_keys": sorted(call.get("args", {})),
                "status": result.get("status"),
                "error_class": error_class(str(result.get("error") or "")) if result.get("status") != "ok" else None,
                "observed_at": checkpoint["ts"],
                "elapsed_ms": result.get("metadata", {}).get("elapsed_ms"),
                "step": state.get("step_count"),
                "supervisor_at": None,
                "supervisor_decision": None,
            })
        seen_observations = len(observed)
        decision = state.get("decision")
        channels = checkpoint.get("updated_channels", [])
        # supervisor output routes to planner/responder; executor may also set
        # terminal decisions. Do not count those as review of a previous call.
        if decision and any(x in channels for x in ("branch:to:planner", "branch:to:responder")):
            decisions.append({"decision": decision, "at": checkpoint["ts"]})
            if calls and calls[-1]["step"] == state.get("step_count") and calls[-1]["supervisor_at"] is None:
                calls[-1]["supervisor_at"] = checkpoint["ts"]
                calls[-1]["supervisor_decision"] = decision
    repeated = 0
    recovered = 0
    for previous, current in zip(calls, calls[1:]):
        same = previous["tool"] == current["tool"] and previous["arg_fingerprint"] == current["arg_fingerprint"]
        if same and previous["status"] != "ok":
            repeated += 1
            recovered += current["status"] == "ok"
    return {
        "decision": final.get("decision"), "steps": final.get("step_count"),
        "calls": calls, "decisions": decisions,
        "identical_call_repeats_after_error": repeated,
        "adjacent_identical_call_recoveries": recovered,
        "done_after_tool_error": final.get("decision") == "done" and any(c["status"] != "ok" for c in calls),
        "no_tool_done": final.get("decision") == "done" and not calls,
        "started_at": checkpoints[0]["ts"], "ended_at": checkpoints[-1]["ts"],
    }


def durable_evidence(db: sqlite3.Connection) -> dict[str, Any]:
    """Prefer direct receipts where present; never add checkpoint copies.

    The table coverage is explicit because historical rows and quick paths
    may predate instrumentation. Receipt status is not outcome verification.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result: dict[str, Any] = {
        "attempt_table_present": "operation_attempts" in tables,
        "decision_table_present": "operation_decisions" in tables,
        "attempts": 0, "linked_goals": 0, "without_goal": 0,
        "attempt_states": {}, "policy_reasons": {},
        "verified_success_rate": None,
        "result_persisted_to_decision_ms": distribution([]),
    }
    if result["attempt_table_present"]:
        result["attempt_states"] = dict(db.execute(
            "SELECT state, COUNT(*) FROM operation_attempts GROUP BY state"))
        result["attempts"] = sum(result["attempt_states"].values())
        result["linked_goals"] = db.execute(
            "SELECT COUNT(DISTINCT goal_id) FROM operation_attempts WHERE goal_id<>''").fetchone()[0]
        result["without_goal"] = db.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE goal_id=''").fetchone()[0]
    if result["decision_table_present"]:
        result["policy_reasons"] = dict(db.execute(
            "SELECT reason_code, COUNT(*) FROM operation_decisions GROUP BY reason_code"))
    if result["attempt_table_present"] and result["decision_table_present"]:
        latencies = [row[0] * 1000 for row in db.execute("""
            SELECT MIN(d.created_at)-a.finished_at
            FROM operation_attempts a JOIN operation_decisions d ON d.attempt_id=a.attempt_id
            WHERE a.finished_at IS NOT NULL AND d.created_at>=a.finished_at
            GROUP BY a.attempt_id,a.finished_at
        """)]
        result["result_persisted_to_decision_ms"] = distribution(latencies)
    result["preferred_call_evidence"] = "operation_attempts" if result["attempts"] else "legacy_checkpoints"
    result["coverage_note"] = "Direct receipts cover only instrumented calls; legacy counts are separate, not additive."
    return result


def audit(db_path: str, graph_path: str) -> dict[str, Any]:
    db = read_snapshot(db_path)
    graph = read_snapshot(graph_path)
    try:
        goals = [dict(r) for r in db.execute("SELECT * FROM goals ORDER BY created_at")]
        statuses = Counter(g["status"] for g in goals)
        traces: dict[str, list[dict]] = defaultdict(list)
        decode_errors = 0
        damaged_threads: set[str] = set()
        rows = graph.execute("SELECT thread_id,type,checkpoint FROM checkpoints WHERE checkpoint_ns='' ORDER BY checkpoint_id")
        for row in rows:
            try:
                traces[row["thread_id"]].append(decode_checkpoint(row["type"], row["checkpoint"]))
            except (ValueError, TypeError, KeyError):
                decode_errors += 1
                damaged_threads.add(row['thread_id'])
        cases = []
        unmatched = 0
        invalid_traces = 0
        for goal in goals:
            key = f"{goal['user_id']}:{goal['id']}"
            if key in damaged_threads:
                invalid_traces += 1
                continue
            checkpoints = traces.get(key)
            if not checkpoints:
                unmatched += 1
                continue
            try:
                case = summarize_trace(checkpoints)
            except (ValueError, TypeError, KeyError):
                invalid_traces += 1
                continue
            case["goal_id"] = goal["id"]
            case["stored_status"] = goal["status"]
            cases.append(case)
        calls = [call for case in cases for call in case["calls"]]
        failures = [call for call in calls if call["status"] != "ok"]
        latencies = [
            (seconds(c["supervisor_at"]) - seconds(c["observed_at"])) * 1000
            for c in failures if c["supervisor_at"]
        ]
        terminal_delays = [
            seconds(case['ended_at']) - seconds(call['observed_at'])
            for case in cases if case['decision'] in {'done', 'fail'}
            for call in case['calls'] if call['status'] != 'ok'
        ]
        audits = [dict(r) for r in db.execute("""
            SELECT tool_name, decision,
                   CASE WHEN details IN ('status=ok', 'status=error',
                        'approval_token_present=true', 'approval_token_present=false')
                        THEN details ELSE 'omitted' END AS safe_details,
                   COUNT(*) AS n
            FROM operation_audit GROUP BY tool_name, decision, safe_details
        """)]
        memory = [dict(r) for r in db.execute("SELECT tool_name,pattern_type,COUNT(*) AS n FROM patterns GROUP BY tool_name,pattern_type")]
        return {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source": {"db": db_path, "graph": graph_path, "read_only": True},
            "window_unix": [min((g['created_at'] for g in goals), default=None), max((g['updated_at'] for g in goals), default=None)],
            "goals": {"n": len(goals), "statuses": dict(statuses),
                      "status_done_rate": statuses['DONE'] / len(goals) if goals else None,
                      "verified_success_rate": None,
                      "nonempty_tool_calls": sum(g.get('tool_calls') not in (None, '', '[]') for g in goals),
                      "nonzero_retry_count": sum(bool(g.get('retry_count')) for g in goals)},
            "coverage": {"matched_goals": len(cases), "unmatched_goals": unmatched,
                         "unlinked_graph_threads": len(set(traces) - {f"{g['user_id']}:{g['id']}" for g in goals}),
                         "decode_errors": decode_errors, "invalid_traces": invalid_traces},
            "tools": {"calls": len(calls), "ok": sum(c['status'] == 'ok' for c in calls),
                      "by_tool": dict(Counter(c['tool'] for c in calls)),
                      "errors": dict(Counter(c['error_class'] for c in failures))},
            "decisions": dict(Counter(d['decision'] for c in cases for d in c['decisions'])),
            "no_tool_done": sum(c['no_tool_done'] for c in cases),
            "done_after_tool_error": sum(c['done_after_tool_error'] for c in cases),
            "failure_observation_to_supervisor_ms": distribution(latencies),
            "failure_observation_to_graph_end_seconds": distribution(terminal_delays),
            "failure_to_human_delivery_ms": None,
            "recovery": {"identical_call_repeats": sum(c['identical_call_repeats_after_error'] for c in cases),
                         "adjacent_identical_call_recoveries": sum(c['adjacent_identical_call_recoveries'] for c in cases),
                         "verified_recovery_retry_distribution": None},
            "operation_audit": audits,
            "durable_evidence": durable_evidence(db),
            "memory": {"patterns": memory, "context_summaries": db.execute('SELECT COUNT(*) FROM context_summaries').fetchone()[0],
                       "retrieval_precision": None, "durable_recall_rate": None},
            "cases": cases,
            "limitations": [
                "DONE and tool ok are not independent outcome verification.",
                "Supervisor latency starts at persisted error observation, not physical failure onset.",
                "Repeated identical calls are a proxy, not a retry episode ID.",
                "Quick commands and external memory may bypass these tables.",
                "Arguments, prompts, model reasoning and user identifiers are intentionally omitted.",
                "Two DB snapshots are individually consistent, not cross-database atomic.",
            ],
        }
    finally:
        db.close()
        graph.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--graph', required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.db, args.graph), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
