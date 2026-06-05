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

                self.assertEqual([row["event"] for row in reversed(rows)], ["warn one", "error two"])
            finally:
                handler.close()


if __name__ == "__main__":
    unittest.main()
