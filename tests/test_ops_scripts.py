from __future__ import annotations

import unittest
from pathlib import Path


class OpsScriptTests(unittest.TestCase):
    def test_deploy_uploads_pkb_client(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("tools/pkb_tools.py", source)

    def test_scripts_use_vps_runuser_absolute_path(self) -> None:
        ops_dir = Path(__file__).resolve().parents[1] / "ops"
        for name in ("backup", "repair", "upgrade", "rollback"):
            source = (ops_dir / f"luck-agent-{name}").read_text(encoding="utf-8")
            self.assertNotIn("/usr/bin/runuser", source)
            self.assertIn("/usr/sbin/runuser", source)
