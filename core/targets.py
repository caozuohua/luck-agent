from __future__ import annotations

from dataclasses import asdict, dataclass


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
