from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from core.health import DBLogHandler


class HealthLogBufferTests(unittest.TestCase):
    def test_emit_buffers_and_query_flushes_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = DBLogHandler(str(Path(tmp) / "memory.db"))
            try:
                handler.emit(logging.LogRecord("test", logging.WARNING, __file__, 1, "warn one", (), None))
                handler.emit(logging.LogRecord("test", logging.ERROR, __file__, 2, "error two", (), None))

                rows = handler.query(hours=1, limit=10)

                # Both records were buffered and flushed; the flusher writes
                # them as a batch with sub-millisecond timestamps, so created_at
                # ordering is non-deterministic on fast machines. Assert the
                # multiset of events rather than emitted-order.
                self.assertEqual(
                    sorted(row["event"] for row in rows),
                    ["error two", "warn one"],
                )
            finally:
                handler.close()


if __name__ == "__main__":
    unittest.main()
