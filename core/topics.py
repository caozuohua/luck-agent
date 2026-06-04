from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any


_ACRONYMS = {
    "ai",
    "api",
    "css",
    "gcp",
    "gpt",
    "html",
    "js",
    "llm",
    "ml",
    "nlp",
    "sql",
    "ts",
    "ui",
    "ux",
}


def normalize_topic(topic: Any) -> str:
    text = str(topic or "").strip().lstrip("#").strip()
    if not text:
        return ""

    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", text):
        return text

    def normalize_part(match: re.Match[str]) -> str:
        part = match.group(0)
        lowered = part.lower()
        if lowered in _ACRONYMS:
            return lowered.upper()
        if part.islower():
            return part[:1].upper() + part[1:]
        return part

    return re.sub(r"[A-Za-z]+", normalize_part, text)


def normalize_topics(topics: Iterable[Any] | str) -> list[str]:
    if isinstance(topics, str):
        topics = re.split(r"[,|\n]", topics)

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_topic in topics:
        topic = normalize_topic(raw_topic)
        if not topic:
            continue
        key = topic.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(topic)
    return normalized
