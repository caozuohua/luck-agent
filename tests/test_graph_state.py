from __future__ import annotations

from core.graph.state import AgentState


def test_state_minimal_seed_is_valid() -> None:
    # total=False -> a goal/user_id seed is a valid AgentState dict.
    s: AgentState = {"goal": "list /tmp", "user_id": "u1"}
    assert s["goal"] == "list /tmp"
    assert s.get("step_count", 0) == 0
    assert s.get("scratchpad", []) == []
    assert s.get("decision") is None
    assert s.get("messages", []) == []
    assert s.get("is_goal_complete", False) is False


def test_append_observation() -> None:
    s: AgentState = {"goal": "x"}
    s.setdefault("scratchpad", []).append({"role": "observation", "content": "file1.txt"})
    assert len(s["scratchpad"]) == 1
    assert s["scratchpad"][0]["content"] == "file1.txt"
