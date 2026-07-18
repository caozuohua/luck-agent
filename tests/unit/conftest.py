from __future__ import annotations

import pytest

from memory.db import Database


@pytest.fixture
async def memory_db() -> Database:
    db = Database(":memory:")
    await db.initialize()
    try:
        yield db
    finally:
        await db.close()
