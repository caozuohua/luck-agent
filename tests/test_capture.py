from __future__ import annotations

from controllers.capture import CaptureDetector, CaptureService
from memory.db import Database


def test_capture_detector_builds_work_note_with_structured_items() -> None:
    draft = CaptureDetector().detect(
        "会议纪要：决定下周发布。行动项：小王准备演示。后续跟进客户反馈。"
    )

    assert draft is not None
    assert draft.kind == "work_note"
    assert draft.decisions == ("会议纪要：决定下周发布",)
    assert draft.actions == ("行动项：小王准备演示",)
    assert draft.follow_ups == ("后续跟进客户反馈",)


def test_capture_detector_routes_idea_and_ignores_commands() -> None:
    detector = CaptureDetector()

    draft = detector.detect("Idea：把服务资产目录做成按目标分页的卡片")

    assert draft is not None
    assert draft.kind == "idea"
    assert detector.detect("/vps service assets") is None
    assert detector.detect("请记住我喜欢简洁回答") is None


async def test_capture_save_is_explicit_and_scoped_to_user_and_chat(tmp_path) -> None:
    db = Database(tmp_path / "capture.db")
    await db.initialize()
    try:
        service = CaptureService(db)
        draft = service.detect("Idea：增加一键回顾")
        assert draft is not None

        pending = service.issue(user_id="u1", chat_id="c1", draft=draft)
        assert service.take(user_id="u1", chat_id="c2", token=pending.token) is None
        assert await db.fetchall("SELECT id FROM captures") == []

        saved = await service.save(pending)
        rows = await db.fetchall(
            "SELECT user_id, chat_id, kind, title FROM captures WHERE id = ?",
            (saved.capture_id,),
        )
        assert [dict(row) for row in rows] == [
            {
                "user_id": "u1",
                "chat_id": "c1",
                "kind": "idea",
                "title": "Idea：增加一键回顾",
            }
        ]

        listed = await service.list_recent(user_id="u1", chat_id="c1")
        assert [item.capture_id for item in listed] == [saved.capture_id]
        assert await service.list_recent(user_id="u1", chat_id="c2") == ()
        assert await service.list_recent(user_id="u2", chat_id="c1") == ()
        assert (
            await service.get(
                user_id="u1",
                chat_id="c1",
                capture_id=saved.capture_id[:8],
            )
        ) == saved
        assert (
            await service.get(
                user_id="u1",
                chat_id="c2",
                capture_id=saved.capture_id[:8],
            )
        ) is None
    finally:
        await db.close()


async def test_capture_list_filters_kind_and_rejects_unknown_kind(tmp_path) -> None:
    db = Database(tmp_path / "capture-filter.db")
    await db.initialize()
    try:
        service = CaptureService(db)
        for text in ("Idea：增加回顾", "会议纪要：决定下周发布"):
            draft = service.detect(text)
            assert draft is not None
            pending = service.issue(user_id="u1", chat_id="c1", draft=draft)
            await service.save(pending)

        ideas = await service.list_recent(user_id="u1", chat_id="c1", kind="idea")
        work = await service.list_recent(user_id="u1", chat_id="c1", kind="work")
        assert [item.draft.kind for item in ideas] == ["idea"]
        assert [item.draft.kind for item in work] == ["work_note"]

        try:
            await service.list_recent(user_id="u1", chat_id="c1", kind="unknown")
        except ValueError as error:
            assert "all, work, or idea" in str(error)
        else:
            raise AssertionError("unknown capture kind must be rejected")
    finally:
        await db.close()
