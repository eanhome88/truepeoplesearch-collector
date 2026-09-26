"""
Unit tests for System Version, Git status, and Auto-Update Engine.
"""

import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

_ROOT_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _ROOT_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import tps_version


class VersionSemverTests(unittest.TestCase):
    def test_parse_semver(self):
        self.assertEqual(tps_version.parse_semver("1.0.0"), (1, 0, 0))
        self.assertEqual(tps_version.parse_semver("v2.3.4"), (2, 3, 4))
        self.assertEqual(tps_version.parse_semver("1.5"), (1, 5, 0))
        self.assertEqual(tps_version.parse_semver(""), (0, 0, 0))
        self.assertEqual(tps_version.parse_semver("v1.2.3-beta"), (1, 2, 3))

    def test_is_version_newer(self):
        self.assertTrue(tps_version.is_version_newer("1.0.1", "1.0.0"))
        self.assertTrue(tps_version.is_version_newer("1.1.0", "1.0.9"))
        self.assertTrue(tps_version.is_version_newer("2.0.0", "1.99.99"))
        self.assertFalse(tps_version.is_version_newer("1.0.0", "1.0.0"))
        self.assertFalse(tps_version.is_version_newer("1.0.0", "1.0.1"))
        self.assertFalse(tps_version.is_version_newer("0.9.9", "1.0.0"))


class VersionInfoTests(unittest.TestCase):
    def test_read_local_version_info(self):
        info = tps_version.read_local_version_info()
        self.assertIsInstance(info, dict)
        self.assertIn("version", info)
        self.assertIn("release_notes", info)
        self.assertTrue(isinstance(info["release_notes"], list))

    def test_get_git_status(self):
        git_info = tps_version.get_git_status()
        self.assertIsInstance(git_info, dict)
        self.assertIn("has_git", git_info)
        self.assertIn("branch", git_info)
        self.assertTrue(git_info["has_git"])

    def test_check_for_updates_local(self):
        res = tps_version.check_for_updates()
        self.assertIsInstance(res, dict)
        self.assertTrue(res.get("ok"))
        self.assertIn("current_version", res)
        self.assertIn("has_update", res)


class VersionApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(_ROOT_DIR / "tools"))
        import dashboard_api
        cls.client = dashboard_api.app.test_client()

    def test_api_system_version(self):
        resp = self.client.get("/api/system/version")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIn("version", data)
        self.assertIn("git", data)

    def test_api_system_check_update(self):
        resp = self.client.get("/api/system/check-update")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIn("has_update", data)
        self.assertIn("current_version", data)

    def test_api_system_apply_update_no_remote(self):
        # Without remote configured, apply update returns a safe error
        with patch("tps_version.get_git_status") as mock_git:
            mock_git.return_value = {"has_git": True, "remote_url": ""}
            resp = self.client.post("/api/system/apply-update", json={})
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data.get("ok"))
            self.assertIn("尚未配置远程源", data.get("error", ""))

    def test_force_update_active(self):
        with patch("tps_version.check_for_updates") as mock_check:
            mock_check.return_value = {"force_update": True, "force_update_reason": "协议升级"}
            active, reason = tps_version.is_force_update_active(use_cache=False)
            self.assertTrue(active)
            self.assertEqual(reason, "协议升级")

    def test_force_update_blocks_start(self):
        with patch("tps_version.is_force_update_active") as mock_force:
            mock_force.return_value = (True, "必须升级")
            resp = self.client.post("/api/cluster/control", json={"action": "start"})
            self.assertEqual(resp.status_code, 426)
            data = resp.get_json()
            self.assertEqual(data.get("code"), "force_update_required")


if __name__ == "__main__":
    unittest.main()
