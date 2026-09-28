"""Static contracts for the supported Windows customer launch paths."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WindowsCustomerLauncherTests(unittest.TestCase):
    def test_batch_launcher_requires_supported_python_and_sets_customer_mode(self):
        source = (ROOT / "start_client.bat").read_text(encoding="utf-8")
        self.assertIn('TPS_RELEASE_MODE=customer', source)
        self.assertIn('TPS_RELEASE_LAUNCH_TOKEN', source)
        self.assertIn('tps_env.py" --dashboard-port', source)
        self.assertIn('customer\' and payload.get(\'launch_token\')', source)
        self.assertIn('sys.version_info >= (3, 9)', source)
        self.assertIn('start --dashboard-only', source)
        self.assertNotIn('http://127.0.0.1:5001', source)
        self.assertNotIn('docker compose', source.casefold())
        self.assertNotIn('git pull', source.casefold())

    def test_stop_batch_only_targets_a_dashboard_only_supervisor(self):
        source = (ROOT / "stop_client.bat").read_text(encoding="utf-8")
        self.assertIn('stop --dashboard-only', source)
        self.assertIn('if errorlevel 1', source.casefold())

    def test_go_launchers_use_the_customer_safe_supervisor_contract(self):
        main = (ROOT / "launcher" / "main.go").read_text(encoding="utf-8")
        stop = (ROOT / "launcher" / "stop.go").read_text(encoding="utf-8")
        self.assertIn('"TPS_RELEASE_MODE=customer"', main)
        self.assertIn('customerDashboardURL', main)
        self.assertIn('customerDashboardIdentityMatches', main)
        self.assertIn('TPS_RELEASE_LAUNCH_TOKEN', main)
        self.assertIn('"start", "--dashboard-only"', main)
        self.assertNotIn('"127.0.0.1:5001"', main)
        self.assertNotIn('syncGitUpdate(rootDir)', main)
        self.assertNotIn('startDocker(rootDir)', main)
        self.assertIn('"stop", "--dashboard-only"', stop)


if __name__ == "__main__":
    unittest.main()
