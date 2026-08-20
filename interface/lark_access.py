from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LarkAccessPolicy:
    """Allow a Lark user or chat before any command reaches the Agent."""

    allowed_user_ids: frozenset[str] = frozenset()
    allowed_chat_ids: frozenset[str] = frozenset()
    allow_unconfigured: bool = False

    def is_allowed(self, *, user_id: str, chat_id: str) -> bool:
        if not self.allowed_user_ids and not self.allowed_chat_ids:
            return self.allow_unconfigured
        return user_id in self.allowed_user_ids or chat_id in self.allowed_chat_ids

    @classmethod
    def from_csv(
        cls,
        *,
        user_ids: str = "",
        chat_ids: str = "",
        allow_unconfigured: bool = False,
    ) -> "LarkAccessPolicy":
        return cls(
            allowed_user_ids=_parse_csv(user_ids),
            allowed_chat_ids=_parse_csv(chat_ids),
            allow_unconfigured=allow_unconfigured,
        )


def _parse_csv(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())
