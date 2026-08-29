from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    intent: str
    skill_name: str = ""
    tool_allowlist: tuple[str, ...] = ()
    requires_confirmation: bool = True
    persistence_target: str = ""
    description: str = ""

@dataclass(frozen=True)
class ScenarioMatch:
    scenario: ScenarioSpec | None
    confidence: float
    reason: str

SCENARIO_CATALOG = (
    ScenarioSpec("work_note", "记录|纪要|会议|任务|决定|行动项", "work_note_capture", ("mem0",), True, "mem0|bitable", "提取决定和行动项"),
    ScenarioSpec("idea_capture", "想法|灵感|点子|创意|备忘|TODO", "idea_capture", ("mem0",), True, "mem0|bitable", "捕获并分类创意"),
    ScenarioSpec("schedule", "日程|提醒|会议|约定|明天|下周", "schedule_management", (), True, "calendar|bitable", "记录时间和事项"),
)
