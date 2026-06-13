from __future__ import annotations


def short_id(value: object, length: int = 4) -> str:
    """Return a compact user-facing identifier without changing storage keys."""
    text = str(value or "").strip()
    if "_" in text:
        prefix, suffix = text.split("_", 1)
        if prefix in {"goal", "step", "task"}:
            text = suffix
    elif "-" in text:
        return text
    return text[: max(1, int(length))]


def matches_short_id(full_id: object, candidate: object) -> bool:
    query = str(candidate or "").strip().lower().lstrip("#")
    text = str(full_id or "").strip().lower()
    if not query:
        return False
    if text == query:
        return True
    if "_" in text and text.split("_", 1)[0] in {"goal", "step", "task"}:
        text = text.split("_", 1)[1]
    return text.startswith(query)
