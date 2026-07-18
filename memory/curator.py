from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from memory.pattern_store import PatternStore


class Curator:
    def __init__(
        self,
        *,
        pattern_store: PatternStore,
        llm_client: Any,
        memory_path: str | Path | None = None,
        memory_max_chars: int = 3000,
        pattern_retention_days: int = 90,
        periodic_interval_seconds: float = 24 * 60 * 60,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        self.pattern_store = pattern_store
        self.llm_client = llm_client
        self.memory_path = Path(memory_path) if memory_path else root / "soul" / "MEMORY.md"
        self.memory_max_chars = memory_max_chars
        self.pattern_retention_days = pattern_retention_days
        self.periodic_interval_seconds = periodic_interval_seconds
        self.last_run_at: float | None = None
        self._periodic_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        patterns = await self.pattern_store.list_patterns()
        if not patterns:
            memory = "暂无长期记忆。"
        else:
            memory = await self._compress_patterns(patterns)
        memory = memory.strip()[: self.memory_max_chars]
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_path.write_text(memory, encoding="utf-8")
        await self._cleanup_old_patterns()
        self.last_run_at = time.time()

    def start_periodic(self) -> asyncio.Task[None]:
        if self._periodic_task is None or self._periodic_task.done():
            self._periodic_task = asyncio.create_task(
                self._periodic_loop(),
                name="curator-periodic",
            )
        return self._periodic_task

    async def stop_periodic(self) -> None:
        if self._periodic_task is None:
            return
        self._periodic_task.cancel()
        try:
            await self._periodic_task
        except asyncio.CancelledError:
            pass
        self._periodic_task = None

    async def _periodic_loop(self) -> None:
        while True:
            await self.run()
            await asyncio.sleep(self.periodic_interval_seconds)

    async def _compress_patterns(self, patterns: list[dict[str, Any]]) -> str:
        lines = []
        for pattern in patterns:
            lines.append(
                "- "
                f"type={pattern.get('pattern_type', '')}; "
                f"tool={pattern.get('tool_name', '')}; "
                f"trigger={pattern.get('trigger', '')}; "
                f"outcome={pattern.get('outcome', '')}"
            )
        task_prompt = (
            "Compress patterns into MEMORY.md. Deduplicate similar items, "
            "keep human-readable operational lessons, and stay under 3000 characters.\n\n"
            + "\n".join(lines)
        )
        return await self.llm_client.generate(
            "You are the luck-agent memory curator.",
            task_prompt,
        )

    async def _cleanup_old_patterns(self) -> None:
        cutoff = int(time.time()) - self.pattern_retention_days * 86400
        await self.pattern_store.delete_older_than(cutoff)
