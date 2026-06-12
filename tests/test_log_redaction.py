from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from core.health import DBLogHandler
from core.log import _JsonFormatter, configure_redaction_secrets


class LogRedactionTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_redaction_secrets(())

    def test_json_formatter_redacts_message_extras_and_exception(self) -> None:
        secret = "configured-log-secret"
        configure_redaction_secrets((secret,))
        try:
            raise RuntimeError(
                "Authorization: Bearer exception-secret "
                f"literal={secret}"
            )
        except RuntimeError:
            exc_info = __import__("sys").exc_info()
        record = logging.LogRecord(
            "third.party",
            logging.ERROR,
            __file__,
            1,
            "connected to wss://example/ws?access_key=message-secret",
            (),
            exc_info,
        )
        record.payload = {
            "client_secret": "extra-secret",
            "safe": "visible",
        }

        output = _JsonFormatter().format(record)
        parsed = json.loads(output)

        for value in (
            "message-secret",
            "extra-secret",
            "exception-secret",
            secret,
        ):
            self.assertNotIn(value, output)
        self.assertEqual(parsed["payload"]["client_secret"], "[REDACTED]")
        self.assertEqual(parsed["payload"]["safe"], "visible")

    def test_db_log_handler_redacts_before_sqlite_write(self) -> None:
        secret = "configured-db-secret"
        configure_redaction_secrets((secret,))
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = DBLogHandler(str(Path(temp_dir) / "memory.db"))
            try:
                try:
                    raise ValueError(
                        "ticket=exception-ticket "
                        f"literal={secret}"
                    )
                except ValueError:
                    exc_info = __import__("sys").exc_info()
                record = logging.LogRecord(
                    "third.party",
                    logging.ERROR,
                    __file__,
                    1,
                    "Authorization: Bearer event-secret",
                    (),
                    exc_info,
                )
                record.user_id = "user-visible"
                record.source = "source-visible"

                handler.emit(record)
                rows = handler.query(hours=1, limit=10)
            finally:
                handler.close()

        serialized = repr(rows)
        for value in (
            "event-secret",
            "exception-ticket",
            secret,
        ):
            self.assertNotIn(value, serialized)
        self.assertIn("[REDACTED]", serialized)


if __name__ == "__main__":
    unittest.main()
