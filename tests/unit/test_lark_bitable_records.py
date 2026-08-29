from interface.lark_platform import _safe_record_summary


def test_record_summary_redacts_sensitive_fields():
    rendered = _safe_record_summary({"标题": "会议", "email": "person@example.com", "token": "secret"})
    assert "会议" in rendered
    assert "person@example.com" not in rendered
    assert "secret" not in rendered
    assert "已脱敏" in rendered


def test_record_summary_truncates_values():
    rendered = _safe_record_summary({"备注": "x" * 400})
    assert len(rendered) < 240
