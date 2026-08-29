from __future__ import annotations
import re
from core.scenarios import SCENARIO_CATALOG, ScenarioMatch, ScenarioSpec

class ScenarioRegistry:
    def __init__(self, scenarios: tuple[ScenarioSpec, ...] = SCENARIO_CATALOG) -> None:
        self._scenarios = {item.scenario_id: item for item in scenarios}
    def get(self, scenario_id: str) -> ScenarioSpec | None:
        return self._scenarios.get(scenario_id)
    def list(self) -> list[ScenarioSpec]:
        return list(self._scenarios.values())
    def find_compatible_skills(self, scenario_id: str) -> list[str]:
        spec = self.get(scenario_id)
        return [spec.skill_name] if spec and spec.skill_name else []

class ScenarioMatcher:
    def __init__(self, registry: ScenarioRegistry | None = None) -> None:
        self.registry = registry or ScenarioRegistry()
    def match(self, user_input: str, detected_intent: object | None = None) -> ScenarioMatch:
        text = user_input.casefold()
        best: tuple[ScenarioSpec | None, float, str] = (None, 0.0, "未匹配场景")
        for spec in self.registry.list():
            keywords = [x for x in re.split(r"[|,]", spec.intent) if x]
            hits = [x for x in keywords if x.casefold() in text]
            score = min(0.98, 0.45 + 0.15 * len(hits)) if hits else 0.0
            if score > best[1]:
                best = (spec, score, f"命中关键词：{'、'.join(hits)}")
        return ScenarioMatch(*best)
