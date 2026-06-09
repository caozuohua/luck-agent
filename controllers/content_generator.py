from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


BLOG_GENERATION_SYSTEM = """你是博客内容策划助手。
严格根据用户原始请求生成可直接展示的结果，并遵循用户请求的语言。
如果用户要求选题，给出 5-10 个具体选题，每项包含标题、切入点和目标读者。
不要声称已写文件、提交代码或发布文章，除非后续工具步骤真实完成。"""


class ModelRouterProtocol(Protocol):
    async def chat(
        self,
        model_name: str,
        messages: list[dict],
        tools_schema: list[dict] | None = None,
        system: str = "",
        user_id: str = "",
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GeneratedContent:
    text: str
    model: str = ""
    tokens: int = 0


class ModelContentGenerator:
    def __init__(
        self,
        *,
        router: ModelRouterProtocol,
        model_name: str,
    ) -> None:
        self.router = router
        self.model_name = model_name

    async def generate(self, goal: dict[str, Any]) -> GeneratedContent:
        source_message = str(
            (goal.get("plan") or {}).get("source_message") or ""
        ).strip()
        if not source_message:
            source_message = str(goal.get("title") or "").strip()
        if not source_message:
            raise ValueError("goal source message is empty")

        result = await self.router.chat(
            model_name=self.model_name,
            messages=[{"role": "user", "content": source_message}],
            tools_schema=[],
            system=BLOG_GENERATION_SYSTEM,
            user_id=str(goal.get("user_id") or ""),
        )
        text = str(result.get("text") or "").strip()
        if not text:
            raise ValueError("model returned empty content")

        return GeneratedContent(
            text=text,
            model=str(result.get("model") or self.model_name),
            tokens=int(result.get("tokens") or 0),
        )
