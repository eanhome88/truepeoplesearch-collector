#!/usr/bin/env python3
"""主机优化脚本静态回归（mac 上跑不了 ps1，只验清单与关键动作都在）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = os.path.join(REPO, "deploy", "windows-full", "Optimize-WindowsHost.ps1")


class WindowsHostTests(unittest.TestCase):
    def test_in_bundle_allowlist(self):
        from package_windows_full_bundle import APP_ALLOWLIST
        self.assertIn("deploy/windows-full/Optimize-WindowsHost.ps1", APP_ALLOWLIST)

    def test_bom_and_markers(self):
        raw = open(PS1, "rb").read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "中文 ps1 必须带 BOM")
        text = raw.decode("utf-8-sig")
        for marker in (
            "SupportsShouldProcess",
            "Administrator",
            "Add-MpPreference",
            "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
            "dynamicport tcp start=1025 num=60000",
            "TcpTimedWaitDelay",
            "NoAutoRebootWithLoggedOnUsers",
            "w32tm /resync",
            "Get-NormalizedInstallRoot",
        ):
            self.assertIn(marker, text, marker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
