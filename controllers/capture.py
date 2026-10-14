from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass

from memory.db import Database


@dataclass(frozen=True)
class CaptureDraft:
    """A structured, not-yet-persisted work note or idea."""

    kind: str
    title: str
    summary: str
    decisions: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    follow_ups: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    raw_input: str = ""

    @property
    def kind_label(self) -> str:
        return "工作纪要" if self.kind == "work_note" else "Idea"


@dataclass(frozen=True)
class PendingCapture:
    token: str
    user_id: str
    chat_id: str
    draft: CaptureDraft
    expires_at: float


@dataclass(frozen=True)
class SavedCapture:
    capture_id: str
    user_id: str
    chat_id: str
    draft: CaptureDraft
    created_at: int = 0


class CaptureDetector:
    """Small deterministic first-pass router for the P0 capture flow."""

    _IDEA_TERMS = (
        "idea",
        "想法",
        "灵感",
        "点子",
        "创意",
        "提议",
        "构思",
    )
    _WORK_TERMS = (
        "会议",
        "纪要",
        "工作记录",
        "周会",
        "同步",
        "讨论",
        "决定",
        "行动项",
        "待办",
        "跟进",
        "meeting",
        "decision",
        "action item",
        "follow up",
    )
    _DECISION_TERMS = ("决定", "结论", "decision")
    _ACTION_TERMS = ("行动项", "待办", "需要", "负责", "action item", "todo")
    _FOLLOW_UP_TERMS = ("跟进", "后续", "提醒", "follow up", "follow-up")

    def __init__(self, *, max_chars: int = 2000) -> None:
        self.max_chars = max(200, max_chars)

    def detect(self, text: str) -> CaptureDraft | None:
        raw = " ".join(str(text or "").strip().split())
        lowered = raw.lower()
        if not raw or raw.startswith("/") or lowered.startswith("mem0 "):
            return None
        idea_score = sum(term in lowered for term in self._IDEA_TERMS)
        work_score = sum(term in lowered for term in self._WORK_TERMS)
        if not idea_score and not work_score:
            return None
        explicit_idea = lowered.startswith(("idea", "想法", "灵感", "点子", "创意"))
        kind = "idea" if explicit_idea or idea_score > work_score else "work_note"
        parts = self._parts(raw)
        summary = self._limit(parts[0] if parts else raw, 240)
        decisions = self._classify(parts, self._DECISION_TERMS)
        actions = self._classify(parts, self._ACTION_TERMS)
        follow_ups = self._classify(parts, self._FOLLOW_UP_TERMS)
        title = self._limit(summary, 80)
        tags = ("idea",) if kind == "idea" else ("work", "note")
        return CaptureDraft(
            kind=kind,
            title=title,
            summary=summary,
            decisions=decisions,
            actions=actions,
            follow_ups=follow_ups,
            tags=tags,
            raw_input=raw[: self.max_chars],
        )

    @staticmethod
    def _parts(text: str) -> list[str]:
        parts = re.split(r"[\n。！？!?；;]+", text)
        return [re.sub(r"^[\s\-•·\d.、]+", "", part).strip() for part in parts if part.strip()]

    @staticmethod
    def _classify(parts: list[str], terms: tuple[str, ...]) -> tuple[str, ...]:
        selected: list[str] = []
        for part in parts:
            lowered = part.lower()
            if any(term in lowered for term in terms) and part not in selected:
                selected.append(part[:240])
        return tuple(selected[:5])

    @staticmethod
    def _limit(value: str, size: int) -> str:
        value = " ".join(value.split()).strip()
        return value if len(value) <= size else value[: size - 1].rstrip() + "…"


class CaptureService:
    """Own pending capture confirmations and explicit SQLite persistence."""

    def __init__(
        self,
        db: Database,
        *,
        detector: CaptureDetector | None = None,
        ttl_seconds: float = 600.0,
    ) -> None:
        self.db = db
        self.detector = detector or CaptureDetector()
        self.ttl_seconds = max(60.0, ttl_seconds)
        self._pending: dict[tuple[str, str], PendingCapture] = {}

    def detect(self, text: str) -> CaptureDraft | None:
        return self.detector.detect(text)

    def issue(self, *, user_id: str, chat_id: str, draft: CaptureDraft) -> PendingCapture:
        self._purge()
        pending = PendingCapture(
            token=secrets.token_urlsafe(6),
            user_id=str(user_id or "default"),
            chat_id=str(chat_id or ""),
            draft=draft,
            expires_at=time.time() + self.ttl_seconds,
        )
        self._pending[(pending.user_id, pending.token.lower())] = pending
        return pending

    def take(self, *, user_id: str, chat_id: str, token: str) -> PendingCapture | None:
        self._purge()
        key = (str(user_id or "default"), token.strip().lower())
        pending = self._pending.get(key)
        if pending is None or pending.chat_id != str(chat_id or ""):
            return None
        self._pending.pop(key, None)
        return pending

    def discard(self, *, user_id: str, chat_id: str, token: str) -> bool:
        return self.take(user_id=user_id, chat_id=chat_id, token=token) is not None

    async def save(self, pending: PendingCapture) -> SavedCapture:
        capture_id = uuid.uuid4().hex
        draft = pending.draft
        created_at = int(time.time())
        await self.db.execute(
            """
            INSERT INTO captures (
                id, user_id, chat_id, kind, title, summary, decisions, actions,
                follow_ups, tags, raw_input, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture_id,
                pending.user_id,
                pending.chat_id,
                draft.kind,
                draft.title,
                draft.summary,
                json.dumps(draft.decisions, ensure_ascii=False),
                json.dumps(draft.actions, ensure_ascii=False),
                json.dumps(draft.follow_ups, ensure_ascii=False),
                json.dumps(draft.tags, ensure_ascii=False),
                draft.raw_input,
                created_at,
            ),
        )
        return SavedCapture(capture_id, pending.user_id, pending.chat_id, draft, created_at)

    async def list_recent(
        self,
        *,
        user_id: str,
        chat_id: str,
        kind: str = "",
        limit: int = 10,
    ) -> tuple[SavedCapture, ...]:
        normalized_kind = self._normalize_kind(kind)
        bounded_limit = max(1, min(int(limit), 10))
        parameters: list[object] = [str(user_id or "default"), str(chat_id or "")]
        kind_clause = ""
        if normalized_kind:
            kind_clause = " AND kind = ?"
            parameters.append(normalized_kind)
        parameters.append(bounded_limit)
        rows = await self.db.fetchall(
            f"""
            SELECT * FROM captures
            WHERE user_id = ? AND chat_id = ?{kind_clause}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            tuple(parameters),
        )
        return tuple(self._row_to_saved(row) for row in rows)

    async def get(
        self,
        *,
        user_id: str,
        chat_id: str,
        capture_id: str,
    ) -> SavedCapture | None:
        reference = str(capture_id or "").strip().lower()
        if len(reference) < 4 or not re.fullmatch(r"[0-9a-f]+", reference):
            return None
        rows = await self.db.fetchall(
            """
            SELECT * FROM captures
            WHERE user_id = ? AND chat_id = ? AND lower(id) LIKE ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 2
            """,
            (str(user_id or "default"), str(chat_id or ""), f"{reference}%"),
        )
        if len(rows) != 1:
            return None
        return self._row_to_saved(rows[0])

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        normalized = str(kind or "").strip().lower()
        aliases = {
            "": "",
            "all": "",
            "全部": "",
            "work": "work_note",
            "work_note": "work_note",
            "工作": "work_note",
            "纪要": "work_note",
            "idea": "idea",
            "ideas": "idea",
            "想法": "idea",
        }
        if normalized not in aliases:
            raise ValueError("capture kind must be all, work, or idea")
        return aliases[normalized]

    @staticmethod
    def _row_to_saved(row) -> SavedCapture:
        def values(name: str) -> tuple[str, ...]:
            try:
                parsed = json.loads(str(row[name] or "[]"))
            except (json.JSONDecodeError, TypeError):
                return ()
            if not isinstance(parsed, list):
                return ()
            return tuple(str(item) for item in parsed if str(item).strip())

        draft = CaptureDraft(
            kind=str(row["kind"]),
            title=str(row["title"]),
            summary=str(row["summary"]),
            decisions=values("decisions"),
            actions=values("actions"),
            follow_ups=values("follow_ups"),
            tags=values("tags"),
            raw_input=str(row["raw_input"]),
        )
        return SavedCapture(
            capture_id=str(row["id"]),
            user_id=str(row["user_id"]),
            chat_id=str(row["chat_id"]),
            draft=draft,
            created_at=int(row["created_at"] or 0),
        )

    def _purge(self) -> None:
        now = time.time()
        for key, pending in list(self._pending.items()):
            if pending.expires_at <= now:
                self._pending.pop(key, None)
