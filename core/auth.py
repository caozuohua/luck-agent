from __future__ import annotations

from collections.abc import Collection


def normalize_admin_users(value: str | Collection[str] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    else:
        parts = list(value)
    return {str(part).strip() for part in parts if str(part).strip()}


def is_authorized_user(config, user_id: str) -> bool:
    admins = normalize_admin_users(getattr(config, "ADMIN_USERS", set()))
    return bool(admins) and user_id in admins
