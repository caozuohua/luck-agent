from __future__ import annotations

import pytest

from memory.goal_store import GoalStatus, GoalStore, InvalidGoalTransition


@pytest.mark.asyncio
async def test_create_update_get_recent(memory_db) -> None:
    store = GoalStore(memory_db)
    goal = await store.create("u1", "run")
    await store.update_status(goal.id, GoalStatus.ROUTING, intent_type="ACTION")

    recent = await store.get_recent("u1")

    assert recent[0].id == goal.id
    assert recent[0].status is GoalStatus.ROUTING
    assert recent[0].intent_type == "ACTION"


@pytest.mark.asyncio
async def test_status_transition_order_rejects_illegal_jump(memory_db) -> None:
    store = GoalStore(memory_db)
    goal = await store.create("u1", "run")

    with pytest.raises(InvalidGoalTransition):
        await store.update_status(goal.id, GoalStatus.DONE)


@pytest.mark.asyncio
async def test_get_in_progress_returns_only_non_terminal_goals(memory_db) -> None:
    store = GoalStore(memory_db)
    running = await store.create("u1", "running")
    done = await store.create("u1", "done")
    failed = await store.create("u1", "failed")
    await store.update_status(running.id, GoalStatus.ROUTING)
    await store.update_status(done.id, GoalStatus.ROUTING)
    await store.update_status(done.id, GoalStatus.PLANNING)
    await store.update_status(done.id, GoalStatus.EXECUTING)
    await store.update_status(done.id, GoalStatus.AWAITING_RESULT)
    await store.update_status(done.id, GoalStatus.EVALUATING)
    await store.update_status(done.id, GoalStatus.DONE)
    await store.update_status(failed.id, GoalStatus.ROUTING)
    await store.update_status(failed.id, GoalStatus.PLANNING)
    await store.update_status(failed.id, GoalStatus.FAILED)

    in_progress = await store.get_in_progress("u1")

    assert [goal.id for goal in in_progress] == [running.id]
