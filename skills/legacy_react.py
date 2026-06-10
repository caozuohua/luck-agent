from __future__ import annotations

from skills.base import SkillContext, SkillMatch, SkillMetadata


class LegacyReactSkill:
    metadata = SkillMetadata(
        name="legacy_react",
        version="1.0.0",
        intent="general",
        description="Explicit fallback to the legacy inline ReAct flow.",
        execution_mode="legacy_inline",
        priority=10000,
    )

    def match(self, context: SkillContext) -> SkillMatch:
        return SkillMatch(False, reason="explicit fallback only")
