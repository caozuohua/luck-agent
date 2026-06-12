from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory import Memory


class MemorySecurityTests(unittest.TestCase):
    def test_memory_restricts_database_permissions_after_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "memory.db")
            with patch("core.memory.os.chmod") as chmod:
                memory = Memory(db_path)
                try:
                    chmod.assert_called_with(db_path, 0o600)
                finally:
                    connection = getattr(memory._local, "conn", None)
                    if connection is not None:
                        connection.close()


if __name__ == "__main__":
    unittest.main()
