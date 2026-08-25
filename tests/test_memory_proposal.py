from memory.proposal import MemoryProposalDetector


def test_detector_extracts_explicit_chinese_request_without_side_effects() -> None:
    detector = MemoryProposalDetector()

    proposal = detector.detect("请记住：我偏好简洁回答")

    assert proposal is not None
    assert proposal.content == "我偏好简洁回答"
    assert proposal.save_command == "/mem0 save 我偏好简洁回答"


def test_detector_extracts_preference_and_ignores_commands() -> None:
    detector = MemoryProposalDetector()

    assert detector.detect("我通常使用 GCP") is not None
    assert detector.detect("/mem0 save 我通常使用 GCP") is None
    assert detector.detect("/vps status") is None
    assert detector.detect("帮我查看服务器状态") is None
