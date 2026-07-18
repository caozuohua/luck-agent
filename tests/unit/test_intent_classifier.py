from __future__ import annotations

from core.intent_classifier import IntentClassifier
from core.output_parser import IntentType


def test_rules_hit_expected_intent_types() -> None:
    classifier = IntentClassifier()

    assert classifier.classify("hello") is IntentType.CHAT
    assert classifier.classify("帮我安排明天会议") is IntentType.ACTION
    assert classifier.classify("search docs") is IntentType.ACTION
    assert classifier.classify("这个怎么弄") is IntentType.CLARIFY


def test_no_rule_match_falls_back_to_chat() -> None:
    assert IntentClassifier().classify("tell me a joke") is IntentType.CHAT


def test_empty_text_falls_back_to_clarify() -> None:
    assert IntentClassifier().classify("   ") is IntentType.CLARIFY
