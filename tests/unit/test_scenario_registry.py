from core.scenario_registry import ScenarioMatcher, ScenarioRegistry


def test_work_note_match():
    match = ScenarioMatcher().match("记录今天的会议纪要和行动项")
    assert match.scenario is not None
    assert match.scenario.scenario_id == "work_note"
    assert match.confidence > 0


def test_registry_skill_lookup():
    registry = ScenarioRegistry()
    assert registry.find_compatible_skills("idea_capture") == ["idea_capture"]
    assert registry.get("missing") is None
