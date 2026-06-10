from __future__ import annotations

import json
import math
import time
from typing import Any

from core.log import get_logger
from core.protocols import new_id

log = get_logger()


class RuntimeEventRecorder:
    def __init__(
        self,
        memory: Any,
        max_string_length: int = 2000,
        max_depth: int = 8,
        max_nodes: int = 500,
        max_payload_bytes: int = 16384,
    ) -> None:
        self._memory = memory
        self._max_string_length = max(0, int(max_string_length))
        self._max_depth = max(0, int(max_depth))
        self._max_nodes = max(1, int(max_nodes))
        self._max_payload_bytes = max(1, int(max_payload_bytes))

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    def _clean_payload(
        self,
        value: Any,
        *,
        depth: int = 0,
        active_ids: set[int] | None = None,
        node_count: list[int] | None = None,
    ) -> Any:
        if depth > self._max_depth:
            return "<max-depth>"
        if active_ids is None:
            active_ids = set()
        if node_count is None:
            node_count = [0]
        node_count[0] += 1
        if node_count[0] > self._max_nodes:
            return "<max-nodes>"
        if isinstance(value, str):
            return value[:self._max_string_length]
        if isinstance(value, float):
            if math.isnan(value):
                return "NaN"
            if math.isinf(value):
                return "Infinity" if value > 0 else "-Infinity"
            return value
        if isinstance(value, dict):
            identity = id(value)
            if identity in active_ids:
                return "<circular>"
            active_ids.add(identity)
            try:
                cleaned = {}
                for key, item in value.items():
                    cleaned[str(key)[:self._max_string_length]] = self._clean_payload(
                        item,
                        depth=depth + 1,
                        active_ids=active_ids,
                        node_count=node_count,
                    )
                    if node_count[0] > self._max_nodes:
                        break
                return cleaned
            finally:
                active_ids.remove(identity)
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in active_ids:
                return "<circular>"
            active_ids.add(identity)
            try:
                cleaned = []
                for item in value:
                    cleaned.append(
                        self._clean_payload(
                            item,
                            depth=depth + 1,
                            active_ids=active_ids,
                            node_count=node_count,
                        )
                    )
                    if node_count[0] > self._max_nodes:
                        break
                return cleaned
            finally:
                active_ids.remove(identity)
        if value is None or isinstance(value, (bool, int)):
            return value
        return str(value)[:self._max_string_length]

    def _bounded_payload(self, value: Any) -> Any:
        cleaned = self._clean_payload(value)
        serialized = self._json_dumps(cleaned)
        if len(serialized.encode("utf-8")) <= self._max_payload_bytes:
            return cleaned

        low = 0
        high = len(serialized)
        truncated = {"truncated": True}
        best: Any = 0
        if len(self._json_dumps(truncated).encode("utf-8")) <= self._max_payload_bytes:
            best = truncated
        while low <= high:
            middle = (low + high) // 2
            candidate = {
                "truncated": True,
                "preview": serialized[:middle],
            }
            size = len(self._json_dumps(candidate).encode("utf-8"))
            if size <= self._max_payload_bytes:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def record(
        self,
        event_type: str,
        *,
        goal_id: str = "",
        step_id: str = "",
        skill: str = "",
        intent: str = "",
        status: str = "",
        user_id: str = "",
        chat_id: str = "",
        payload: Any = None,
        event_id: str | None = None,
        created_at: float | None = None,
    ) -> None:
        try:
            self._memory.append_runtime_event(
                {
                    "event_id": event_id or new_id("event"),
                    "goal_id": goal_id,
                    "step_id": step_id,
                    "skill": skill,
                    "intent": intent,
                    "event_type": event_type,
                    "status": status,
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "payload": self._bounded_payload(
                        {} if payload is None else payload
                    ),
                    "created_at": time.time() if created_at is None else created_at,
                }
            )
        except Exception as exc:
            log.error(
                "runtime_event_write_failed",
                event_type=event_type,
                goal_id=goal_id,
                error=type(exc).__name__,
            )


class NoopRuntimeEventRecorder:
    def record(
        self,
        event_type: str,
        *,
        goal_id: str = "",
        step_id: str = "",
        skill: str = "",
        intent: str = "",
        status: str = "",
        user_id: str = "",
        chat_id: str = "",
        payload: Any = None,
        event_id: str | None = None,
        created_at: float | None = None,
    ) -> None:
        return None
