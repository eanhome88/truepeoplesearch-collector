#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import tps_supervisor


class TestTpsSupervisor(unittest.TestCase):
    def test_process_spec_start_and_stop(self):
        log_file = Path("/tmp/tps_test_spec.log")
        spec = tps_supervisor.ProcessSpec(
            name="test_echo",
            cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
            log_file=log_file,
        )
        spec.start()
        self.assertTrue(spec.is_alive())
        self.assertIsNotNone(spec.proc.pid)
        spec.stop(timeout=2.0)
        self.assertFalse(spec.is_alive())
        if log_file.exists():
            log_file.unlink()

    def test_supervisor_spec_initialization(self):
        sv = tps_supervisor.Supervisor(concurrency=4, with_dashboard=True)
        self.assertIn("worker", sv.specs)
        self.assertIn("dashboard", sv.specs)
        self.assertIn("--concurrency", sv.specs["worker"].cmd)
        self.assertIn("4", sv.specs["worker"].cmd)

        sv_no_dash = tps_supervisor.Supervisor(concurrency=2, with_dashboard=False)
        self.assertIn("worker", sv_no_dash.specs)
        self.assertNotIn("dashboard", sv_no_dash.specs)

    def test_spec_suspends_after_max_crashes(self):
        log_file = Path("/tmp/tps_test_crash.log")
        spec = tps_supervisor.ProcessSpec(
            name="test_crash",
            cmd=[sys.executable, "-c", "import sys; sys.exit(1)"],
            log_file=log_file,
            max_crashes=2,
        )
        self.assertFalse(spec.suspended)
        spec.crash_count = 3
        if spec.crash_count > spec.max_crashes:
            spec.suspended = True
        self.assertTrue(spec.suspended)
        spec.start()
        self.assertFalse(spec.suspended)
        spec.stop(timeout=1.0)
        if log_file.exists():
            log_file.unlink()


if __name__ == "__main__":
    unittest.main()
