from __future__ import annotations

import unittest
from pathlib import Path


class SystemdServiceTests(unittest.TestCase):
    def test_service_allows_sudoers_wrappers(self) -> None:
        service = (
            Path(__file__).resolve().parents[1] / "luck-agent.service"
        ).read_text(encoding="utf-8")

        self.assertIn("User=luck-agent", service)
        self.assertIn("NoNewPrivileges=no", service)
        self.assertNotIn("NoNewPrivileges=yes", service)
        self.assertIn("ProtectSystem=strict", service)
