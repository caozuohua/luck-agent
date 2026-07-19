from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Pattern

from core.output_parser import IntentType


@dataclass(frozen=True)
class IntentRule:
    intent: IntentType
    keywords: tuple[str, ...] = ()
    regexes: tuple[Pattern[str], ...] = field(default_factory=tuple)

    def matches(self, text: str) -> bool:
        lowered = text.lower()
        if any(keyword.lower() in lowered for keyword in self.keywords):
            return True
        return any(regex.search(text) for regex in self.regexes)


class IntentClassifier:
    """Rule-only classifier. It never calls an LLM."""

    def __init__(self, rules: list[IntentRule] | None = None) -> None:
        self.rules = rules or self._default_rules()

    def classify(self, user_input: str) -> IntentType:
        text = (user_input or "").strip()
        if not text:
            return IntentType.CLARIFY
        for rule in self.rules:
            if rule.matches(text):
                return rule.intent
        return IntentType.CHAT

    def _default_rules(self) -> list[IntentRule]:
        clarify_regexes = (
            re.compile(r"^(这个|那个|它|这|那)(怎么|如何|咋).*$"),
            re.compile(r"^\?+$"),
        )
        return [
            IntentRule(
                IntentType.CLARIFY,
                keywords=("不确定", "不清楚", "什么意思", "怎么弄"),
                regexes=clarify_regexes,
            ),
            IntentRule(
                IntentType.ACTION,
                keywords=(
                    "安排",
                    "会议",
                    "日程",
                    "提醒",
                    "搜索",
                    "查找",
                    "文件",
                    "文档",
                    "上传",
                    "github",
                    "部署",
                    "运行",
                    "执行",
                    "shell",
                    "schedule",
                    "calendar",
                    "search",
                    "file",
                    "document",
                    "deploy",
                    "run",
                    # time / date / clock
                    "时间",
                    "几点",
                    "日期",
                    "几号",
                    "星期",
                    "今天",
                    "明天",
                    "昨天",
                    "现在",
                    "当前时间",
                    "time",
                    "date",
                    "clock",
                    "now",
                    # filesystem / shell
                    "目录",
                    "文件夹",
                    "路径",
                    "当前目录",
                    "列出",
                    "文件列表",
                    "pwd",
                    "ls",
                    "cd",
                    "磁盘",
                    "disk",
                    "directory",
                    "folder",
                    "list",
                ),
            ),
        ]
