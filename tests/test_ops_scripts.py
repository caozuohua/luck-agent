from __future__ import annotations

import unittest
from pathlib import Path


class OpsScriptTests(unittest.TestCase):
    def test_deploy_uploads_v2_entrypoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "deploy.sh").read_text(encoding="utf-8")
        # V2 ships main.py as the runtime entrypoint (V1 agent.py retired)
        self.assertIn("main.py", source)
        self.assertIn("luck-agent.service", source)

    def test_scripts_use_vps_runuser_absolute_path(self) -> None:
        ops_dir = Path(__file__).resolve().parents[1] / "ops"
        for name in ("backup", "repair", "upgrade", "rollback"):
            source = (ops_dir / f"luck-agent-{name}").read_text(encoding="utf-8")
            self.assertNotIn("/usr/bin/runuser", source)
            self.assertIn("/usr/sbin/runuser", source)

    def test_restart_install_is_least_privilege(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "ops" / "install-restart.sh").read_text(encoding="utf-8")
        restart = (root / "ops" / "luck-agent-restart").read_text(encoding="utf-8")
        self.assertIn("luck-agent-restart", source)
        self.assertIn("NOPASSWD", source)
        self.assertNotIn("luck-agent-upgrade", source)
        self.assertIn("systemd-run", restart)
        self.assertIn("--on-active=3s", restart)
        self.assertIn("--no-block", restart)
        self.assertIn("--collect", restart)

    def test_deployment_contract_uses_v2_user_and_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env_example = (root / ".env.example").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        deploy = (root / "deploy.sh").read_text(encoding="utf-8")

        self.assertIn("AGENT_WORKDIR=/opt/luck-agent/workspace", env_example)
        self.assertIn("DB_PATH=/opt/luck-agent/data/agent.db", env_example)
        self.assertIn("GRAPH_DB_PATH=/opt/luck-agent/data/graph_state.db", env_example)
        self.assertNotIn("/home/agent", env_example)
        self.assertIn("./data:/opt/luck-agent/data", compose)
        self.assertIn("./workspace:/opt/luck-agent/workspace", compose)
        self.assertNotIn("/home/agent", compose)
        self.assertIn("luck-agent:luck-agent", deploy)
        self.assertNotIn("agent:agent", deploy)

    def test_maintenance_scripts_use_current_v2_database_and_entrypoint(self) -> None:
        ops_dir = Path(__file__).resolve().parents[1] / "ops"
        backup = (ops_dir / "luck-agent-backup").read_text(encoding="utf-8")
        repair = (ops_dir / "luck-agent-repair").read_text(encoding="utf-8")
        restore = (ops_dir / "luck-agent-restore").read_text(encoding="utf-8")
        restore_worker = (ops_dir / "luck-agent-restore-worker").read_text(encoding="utf-8")
        upgrade = (ops_dir / "luck-agent-upgrade").read_text(encoding="utf-8")

        for source in (backup, repair, restore, restore_worker):
            self.assertNotIn("/opt/luck-agent/memory.db", source)
            self.assertIn("/opt/luck-agent/data", source)
        self.assertIn("compileall", upgrade)
        self.assertIn('"$repo/main.py"', upgrade)
        self.assertNotIn('"$repo/agent.py"', upgrade)
        self.assertNotIn('"$repo/config.py"', upgrade)
        self.assertIn("systemctl daemon-reload", upgrade)
