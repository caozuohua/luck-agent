from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import RLock


@dataclass(frozen=True)
class VpsTarget:
    """Stable identity for a VPS before cloud-specific execution is added."""

    provider: str = "aws"
    account: str = ""
    region: str = ""
    target_id: str = ""
    role: str = "personal"

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower() or "other"
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "account", self.account.strip())
        object.__setattr__(self, "region", self.region.strip())
        object.__setattr__(self, "target_id", self.target_id.strip())
        object.__setattr__(self, "role", self.role.strip().lower() or "other")

    @property
    def label(self) -> str:
        return self.target_id or self.account or f"{self.provider}-local"

    @property
    def display(self) -> str:
        parts = [self.provider.upper(), self.label]
        if self.region:
            parts.append(self.region)
        return " / ".join(parts)

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class VpsTargetRegistry:
    """Thread-safe target catalog with per-user active target selection."""

    def __init__(self, targets: list[VpsTarget], *, default_target: str = "") -> None:
        unique: dict[str, VpsTarget] = {}
        for target in targets:
            unique[target.label] = target
        self._targets = unique
        self._default_target = default_target if default_target in unique else next(iter(unique), "")
        self._selected: dict[str, str] = {}
        self._lock = RLock()

    @classmethod
    def from_csv(cls, value: str, *, default_target: VpsTarget) -> "VpsTargetRegistry":
        """Parse ``id|provider|account|region|role;...`` entries."""
        targets = [default_target]
        for raw in value.split(";"):
            fields = [field.strip() for field in raw.split("|")]
            if not fields or not fields[0]:
                continue
            targets.append(
                VpsTarget(
                    target_id=fields[0],
                    provider=fields[1] if len(fields) > 1 and fields[1] else default_target.provider,
                    account=fields[2] if len(fields) > 2 else "",
                    region=fields[3] if len(fields) > 3 else "",
                    role=fields[4] if len(fields) > 4 else "personal",
                )
            )
        return cls(targets, default_target=default_target.label)

    def list(self) -> list[VpsTarget]:
        with self._lock:
            return list(self._targets.values())

    def current(self, user_id: str = "default") -> VpsTarget:
        with self._lock:
            key = self._selected.get(user_id, self._default_target)
            return self._targets[key]

    def select(self, user_id: str, target_id: str) -> VpsTarget | None:
        with self._lock:
            if target_id not in self._targets:
                return None
            self._selected[user_id] = target_id
            return self._targets[target_id]
