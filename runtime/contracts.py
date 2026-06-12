"""Value objects exposed by the Goal Runtime boundary."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True, eq=False)
class RuntimeHandleResult(Mapping[str, Any]):
    """Immutable result of routing one message through Goal Runtime."""

    handled: bool
    skill: str
    goal_id: str
    intent: str
    status: str
    queue_status: str
    summary: str
    reason: str

    _FIELDS: ClassVar[tuple[str, ...]] = (
        "handled",
        "skill",
        "goal_id",
        "intent",
        "status",
        "queue_status",
        "summary",
        "reason",
    )

    def __post_init__(self) -> None:
        if self.handled and not all(
            (self.skill, self.goal_id, self.intent)
        ):
            raise ValueError(
                "handled result requires skill, goal_id, and intent"
            )
        if not self.handled and self.goal_id:
            raise ValueError("fallback result cannot include goal_id")

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self._FIELDS}

    def __getitem__(self, key: str) -> Any:
        if key not in self._FIELDS:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._FIELDS)

    def __len__(self) -> int:
        return len(self._FIELDS)
