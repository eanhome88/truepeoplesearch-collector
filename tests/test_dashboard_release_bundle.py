"""Verify the allowlisted customer bundle can import its dashboard without worker source."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGER_PATH = ROOT / "scripts" / "package_windows_release.py"
SPEC = importlib.util.spec_from_file_location("dashboard_release_packager", PACKAGER_PATH)
assert SPEC is not None and SPEC.loader is not None
packager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = packager
SPEC.loader.exec_module(packager)


class DashboardReleaseBundleTests(unittest.TestCase):
    def test_allowlisted_dashboard_imports_in_customer_mode_without_worker_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            release_root = Path(directory) / "release"
            release_root.mkdir()
            for relative in packager.RUNTIME_SOURCE_ALLOWLIST:
                source = ROOT / relative
                target = release_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            self.assertFalse((release_root / "scripts" / "distributed_worker.py").exists())
            self.assertFalse((release_root / "scripts" / "protocol_worker.py").exists())
            probe = r'''
import importlib.util
from pathlib import Path

path = Path("tools/dashboard_api.py")
spec = importlib.util.spec_from_file_location("release_bundle_dashboard", path)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
client = api.app.test_client()
assert client.get("/api/health").status_code == 200
assert client.get("/api/system/version").get_json()["release_mode"] == "customer"
response = client.post("/api/cluster/control", json={"action": "start"})
assert response.status_code == 403
assert response.get_json()["code"] == "customer_release_control_surface_disabled"
'''
            environment = dict(os.environ)
            environment["TPS_RELEASE_MODE"] = "customer"
            environment["TPS_RELEASE_LAUNCH_TOKEN"] = "bundle_launch_token_123456"
            completed = subprocess.run(
                [sys.executable, "-B", "-c", probe],
                cwd=release_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
