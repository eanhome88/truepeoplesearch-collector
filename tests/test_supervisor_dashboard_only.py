"""Unit tests for the explicit, non-collection supervisor launch mode.

These tests only build process specifications or replace the supervisor with a
fake.  They never launch a subprocess, dashboard, worker, feeder, or network
connection.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tps_supervisor as supervisor


class DashboardOnlySupervisorTests(unittest.TestCase):
    def test_dashboard_only_specs_exclude_collection_processes_and_force_loopback(self):
        with mock.patch.dict(os.environ, {"TPS_DASHBOARD_HOST": "::1"}, clear=False):
            args = supervisor._parse_args(["start", "--dashboard-only"])
            instance = supervisor._build_supervisor(args)

        self.assertEqual(set(instance.specs), {"dashboard"})
        self.assertFalse(instance.with_worker)
        self.assertFalse(instance.with_feeder)
        self.assertTrue(instance.with_dashboard)

        dashboard_cmd = instance.specs["dashboard"].cmd
        self.assertEqual(dashboard_cmd[dashboard_cmd.index("--host") + 1], "127.0.0.1")
        self.assertIn("--strict-port", dashboard_cmd)
        self.assertNotIn("distributed_worker.py", " ".join(dashboard_cmd))
        self.assertNotIn("phone_discover.py", " ".join(dashboard_cmd))
        self.assertEqual(
            instance.specs["dashboard"].env_overrides,
            {"TPS_RELEASE_MODE": "customer"},
        )

    def test_default_launch_keeps_existing_worker_feeder_and_dashboard_specs(self):
        args = supervisor._parse_args(["start"])
        instance = supervisor._build_supervisor(args)

        self.assertEqual(set(instance.specs), {"worker", "phone_feeder", "dashboard"})

    def test_dashboard_only_cli_invokes_only_a_dashboard_only_supervisor(self):
        constructed = []

        class FakeSupervisor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.ran = False
                constructed.append(self)

            def run(self):
                self.ran = True

        with mock.patch.object(supervisor, "Supervisor", FakeSupervisor):
            with mock.patch.object(sys, "argv", ["tps_supervisor.py", "start", "--dashboard-only"]):
                supervisor.main()

        self.assertEqual(len(constructed), 1)
        self.assertTrue(constructed[0].ran)
        self.assertEqual(
            constructed[0].kwargs,
            {
                "concurrency": mock.ANY,
                "with_dashboard": True,
                "with_feeder": False,
                "with_worker": False,
                "dashboard_host": "127.0.0.1",
            },
        )

    def test_dashboard_only_rejects_conflicting_or_unsafe_actions(self):
        for argv in (
            ["start", "--dashboard-only", "--no-dashboard"],
            ["restart", "--dashboard-only"],
            ["status", "--dashboard-only"],
        ):
            with self.subTest(argv=argv):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        supervisor._parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_customer_process_environment_rejects_a_full_supervisor_start(self):
        with mock.patch.dict(os.environ, {"TPS_RELEASE_MODE": "customer"}, clear=True), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                supervisor._parse_args(["start"])
        self.assertEqual(raised.exception.code, 2)

    def test_dashboard_only_stop_is_an_explicitly_scoped_action(self):
        args = supervisor._parse_args(["stop", "--dashboard-only"])
        self.assertEqual(args.action, "stop")
        self.assertTrue(args.dashboard_only)

    def test_dashboard_only_stop_refuses_any_other_supervisor_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "supervisor.pid"
            pid_file.write_text("12345", encoding="utf-8")
            supervisor._mode_file(pid_file).write_text(
                json.dumps({"pid": 12345, "mode": "full"}), encoding="utf-8"
            )
            with mock.patch.object(supervisor, "PID_FILE", pid_file), \
                    mock.patch.object(supervisor.os, "kill") as kill, \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertFalse(supervisor.cmd_stop(dashboard_only=True))
        kill.assert_not_called()

    def test_dashboard_only_stop_refuses_a_reused_pid_without_matching_process_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "supervisor.pid"
            pid_file.write_text("12345", encoding="utf-8")
            supervisor._mode_file(pid_file).write_text(
                json.dumps({
                    "pid": 12345,
                    "mode": "dashboard-only",
                    "process_start_marker": 1000,
                }),
                encoding="utf-8",
            )
            with mock.patch.object(supervisor, "PID_FILE", pid_file), \
                    mock.patch.object(supervisor, "_process_start_marker", return_value=2000), \
                    mock.patch.object(supervisor.subprocess, "run") as taskkill, \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertFalse(supervisor.cmd_stop(dashboard_only=True))
        taskkill.assert_not_called()

    def test_windows_dashboard_only_stop_uses_a_verified_pid_scoped_process_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "supervisor.pid"
            pid_file.write_text("12345", encoding="utf-8")
            supervisor._mode_file(pid_file).write_text(
                json.dumps({
                    "pid": 12345,
                    "mode": "dashboard-only",
                    "process_start_marker": 1000,
                }),
                encoding="utf-8",
            )
            windows_os = SimpleNamespace(name="nt")
            completed = SimpleNamespace(returncode=0)
            with mock.patch.object(supervisor, "PID_FILE", pid_file), \
                    mock.patch.object(supervisor, "os", windows_os), \
                    mock.patch.object(supervisor, "_process_start_marker", return_value=1000), \
                    mock.patch.object(supervisor.subprocess, "run", return_value=completed) as taskkill, \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertTrue(supervisor.cmd_stop(dashboard_only=True))
        taskkill.assert_called_once_with(
            ["taskkill", "/PID", "12345", "/T", "/F"],
            stdin=supervisor.subprocess.DEVNULL,
            stdout=supervisor.subprocess.DEVNULL,
            stderr=supervisor.subprocess.DEVNULL,
            timeout=10,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
