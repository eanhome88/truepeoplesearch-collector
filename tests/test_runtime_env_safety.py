"""Safe local configuration loader tests; no service or network is used."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tps_env


class RuntimeEnvSafetyTests(unittest.TestCase):
    def _write_envs(self, root: Path, root_text: str, deploy_text: str) -> None:
        (root / ".env").write_text(root_text, encoding="utf-8")
        deploy_dir = root / "deploy"
        deploy_dir.mkdir()
        (deploy_dir / ".env").write_text(deploy_text, encoding="utf-8")

    def test_process_environment_wins_and_loader_never_executes_or_prints_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_envs(
                root,
                "TPS_DB_HOST=root-db\nTPS_DB_PORT=4000\nTPS_SAMPLE=$(never-run)\nTPS_RELEASE_MODE=customer\n",
                "TPS_DB_HOST=deploy-db\nTPS_REDIS_PORT=6388\n",
            )
            environment = {"TPS_DB_PORT": "5999"}
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                tps_env.load_project_env(root, environment, customer_safe=False)

        self.assertEqual(environment["TPS_DB_PORT"], "5999")
        self.assertEqual(environment["TPS_DB_HOST"], "root-db")
        self.assertEqual(environment["TPS_REDIS_PORT"], "6388")
        self.assertEqual(environment["TPS_SAMPLE"], "$(never-run)")
        self.assertNotIn("TPS_RELEASE_MODE", environment)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_customer_file_load_accepts_only_local_runtime_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_envs(
                root,
                "\n".join((
                    "TPS_DB_HOST=file-db",
                    "TPS_DB_PORT=4111",
                    "TPS_REDIS_PORT=6388",
                    "TPS_DASHBOARD_PORT=5123",
                    "TPS_RELEASE_MODE=standard",
                    "TPS_RELEASE_LAUNCH_TOKEN=file-token-that-must-not-load",
                    "TPS_ALERT_WEBHOOK=https://example.invalid/file-alert",
                    "TPS_UPDATE_CHECK_URL=https://example.invalid/file-update",
                    "PROXY_TUNNEL=socks5://example.invalid:1080",
                    "TPS_CONCURRENCY=99",
                )),
                "TPS_DB_NAME=people_search\nTPS_DASHBOARD_PORT=5999\n",
            )
            environment = {
                "TPS_RELEASE_MODE": "customer",
                "TPS_DB_HOST": "process-db",
                "TPS_ALERT_WEBHOOK": "process-owned-alert",
            }
            tps_env.load_project_env(root, environment, customer_safe=True)

        self.assertEqual(environment["TPS_DB_HOST"], "process-db")
        self.assertEqual(environment["TPS_DB_PORT"], "4111")
        self.assertEqual(environment["TPS_REDIS_PORT"], "6388")
        self.assertEqual(environment["TPS_DASHBOARD_PORT"], "5123")
        self.assertEqual(environment["TPS_DB_NAME"], "people_search")
        self.assertEqual(environment["TPS_RELEASE_MODE"], "customer")
        self.assertEqual(environment["TPS_ALERT_WEBHOOK"], "process-owned-alert")
        for name in (
            "TPS_RELEASE_LAUNCH_TOKEN",
            "TPS_UPDATE_CHECK_URL",
            "PROXY_TUNNEL",
            "TPS_CONCURRENCY",
        ):
            self.assertNotIn(name, environment)

    def test_port_and_launch_token_validation_do_not_echo_bad_values(self):
        self.assertEqual(tps_env.dashboard_port({"TPS_DASHBOARD_PORT": "5123"}), 5123)
        for value in ("0", "65536", "not-a-port"):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                tps_env.dashboard_port({"TPS_DASHBOARD_PORT": value})
            self.assertIn("TPS_DASHBOARD_PORT", str(raised.exception))
            self.assertNotIn(value, str(raised.exception))

        self.assertEqual(
            tps_env.release_launch_token({"TPS_RELEASE_LAUNCH_TOKEN": "release_token_123456"}),
            "release_token_123456",
        )
        for value in ("short", "token with space", "token/with/slash"):
            with self.subTest(value=value):
                self.assertIsNone(tps_env.release_launch_token({"TPS_RELEASE_LAUNCH_TOKEN": value}))


if __name__ == "__main__":
    unittest.main()
