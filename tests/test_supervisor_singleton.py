"""Defensive supervisor ownership tests; never launch collection services."""

import errno
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tps_supervisor as supervisor


def _try_singleton_in_child(pid_path, result):
    try:
        # Prove the OS lock itself rejects the contender, independently of PID.
        with mock.patch.object(supervisor, "_pid_is_running", return_value=False):
            with supervisor._SupervisorSingleton(Path(pid_path)):
                result.send("acquired")
    except supervisor.SupervisorAlreadyRunning:
        result.send("blocked")
    finally:
        result.close()


def _hold_singleton_in_child(pid_path, connection):
    with supervisor._SupervisorSingleton(Path(pid_path)):
        connection.send("holding")
        connection.recv()


class SupervisorSingletonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="supervisor-singleton-")
        self.addCleanup(self.temporary.cleanup)
        self.pid_file = Path(self.temporary.name) / "supervisor.pid"
        self.pid_patch = mock.patch.object(supervisor, "PID_FILE", self.pid_file)
        self.pid_patch.start()
        self.addCleanup(self.pid_patch.stop)

    def make_supervisor(self):
        with mock.patch.object(supervisor.Supervisor, "_init_specs"):
            instance = supervisor.Supervisor(concurrency=1)
        instance.specs = {"synthetic": mock.Mock(spec=supervisor.ProcessSpec)}
        return instance

    def test_duplicate_start_does_not_launch_or_overwrite_owner(self):
        instance = self.make_supervisor()
        with supervisor._SupervisorSingleton(self.pid_file):
            original = self.pid_file.read_bytes()
            with mock.patch.object(supervisor.signal, "signal") as install_signal:
                with self.assertRaises(supervisor.SupervisorAlreadyRunning):
                    instance.run()
            self.assertEqual(self.pid_file.read_bytes(), original)
            install_signal.assert_not_called()
            instance.specs["synthetic"].start.assert_not_called()
            instance.specs["synthetic"].stop.assert_not_called()

    def test_lock_blocks_a_separate_process(self):
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        child = context.Process(target=_try_singleton_in_child, args=(str(self.pid_file), send))
        with supervisor._SupervisorSingleton(self.pid_file):
            try:
                child.start()
                send.close()
                self.assertTrue(receive.poll(10), "singleton contender did not report")
                self.assertEqual(receive.recv(), "blocked")
                child.join(10)
                self.assertEqual(child.exitcode, 0)
            finally:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
                receive.close()
                send.close()

    def test_crashed_owner_releases_lock_and_stale_pid_is_recovered(self):
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe()
        child = context.Process(target=_hold_singleton_in_child,
                                args=(str(self.pid_file), child_connection))
        try:
            child.start()
            child_connection.close()
            self.assertTrue(parent_connection.poll(10), "singleton owner did not report")
            self.assertEqual(parent_connection.recv(), "holding")
            self.assertEqual(self.pid_file.read_text(), str(child.pid))
            child.terminate()
            child.join(10)
            self.assertFalse(child.is_alive())
            self.assertTrue(self.pid_file.exists(), "terminated owner leaves stale PID")
            with supervisor._SupervisorSingleton(self.pid_file) as recovered:
                self.assertEqual(self.pid_file.read_text(), str(os.getpid()))
                self.assertFalse(os.get_inheritable(recovered._handle.fileno()))
            self.assertFalse(self.pid_file.exists())
        finally:
            if child.is_alive():
                child.terminate()
                child.join(5)
            parent_connection.close()
            child_connection.close()

    def test_windows_lock_uses_same_byte_and_releases_owner(self):
        locking = mock.Mock(side_effect=[None, OSError(errno.EACCES, "locked"), None])
        windows_locks = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        windows_os = SimpleNamespace(name="nt", SEEK_END=os.SEEK_END, replace=os.replace)
        owner = supervisor._SupervisorSingleton(self.pid_file)
        contender = supervisor._SupervisorSingleton(self.pid_file)
        with mock.patch.object(supervisor, "os", windows_os):
            with mock.patch.dict(sys.modules, {"msvcrt": windows_locks}):
                with owner:
                    with self.assertRaises(supervisor.SupervisorAlreadyRunning):
                        with contender:
                            self.fail("locked Windows singleton accepted")
        self.assertEqual([call.args[1:] for call in locking.call_args_list], [(2, 1), (2, 1), (0, 1)])
        self.assertEqual(owner.lock_file.read_bytes(), b"\0")
        self.assertFalse(self.pid_file.exists())

    def test_live_legacy_pid_is_not_overwritten(self):
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        before = self.pid_file.stat()
        with self.assertRaises(supervisor.SupervisorAlreadyRunning):
            with supervisor._SupervisorSingleton(self.pid_file):
                self.fail("live PID accepted")
        self.assertEqual(self.pid_file.read_text(), str(os.getpid()))
        self.assertEqual(self.pid_file.stat().st_mtime_ns, before.st_mtime_ns)

    def test_stale_pid_is_replaced_and_only_own_record_removed(self):
        self.pid_file.write_text("123456", encoding="utf-8")
        with mock.patch.object(supervisor, "_pid_is_running", return_value=False):
            with supervisor._SupervisorSingleton(self.pid_file) as owner:
                self.assertEqual(self.pid_file.read_text(), str(os.getpid()))
                lock_file = owner.lock_file
            self.assertFalse(self.pid_file.exists())
            self.assertTrue(lock_file.exists(), "lock inode must remain stable")
            with supervisor._SupervisorSingleton(self.pid_file):
                self.assertTrue(self.pid_file.exists())

    def test_replaced_pid_record_is_preserved_even_with_same_pid(self):
        with supervisor._SupervisorSingleton(self.pid_file):
            replacement = self.pid_file.with_name("replacement.pid")
            replacement.write_text(str(os.getpid()), encoding="utf-8")
            os.replace(replacement, self.pid_file)
        self.assertTrue(self.pid_file.exists())

    def test_rewritten_foreign_pid_record_is_preserved(self):
        with supervisor._SupervisorSingleton(self.pid_file):
            self.pid_file.write_text("999999", encoding="utf-8")
        self.assertEqual(self.pid_file.read_text(), "999999")

    def test_partial_start_failure_cleans_up_with_lock_held(self):
        instance = self.make_supervisor()
        failing = mock.Mock(spec=supervisor.ProcessSpec)
        failing.start.side_effect = RuntimeError("synthetic startup failure")
        instance.specs["synthetic_failure"] = failing

        def assert_owned():
            self.assertTrue(self.pid_file.exists())
            with self.assertRaises(supervisor.SupervisorAlreadyRunning):
                with supervisor._SupervisorSingleton(self.pid_file):
                    self.fail("shutdown released ownership too early")

        instance.specs["synthetic"].stop.side_effect = assert_owned
        with mock.patch.object(supervisor.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "synthetic startup failure"):
                instance.run()
        instance.specs["synthetic"].stop.assert_called_once()
        failing.stop.assert_called_once()
        self.assertFalse(self.pid_file.exists())
        with supervisor._SupervisorSingleton(self.pid_file):
            pass

    def test_normal_shutdown_cleans_up_record(self):
        instance = self.make_supervisor()
        instance.stopping = True
        with mock.patch.object(supervisor.signal, "signal"):
            instance.run()
        instance.specs["synthetic"].start.assert_called_once()
        instance.specs["synthetic"].stop.assert_called_once()
        self.assertFalse(self.pid_file.exists())
        self.assertFalse(supervisor._mode_file(self.pid_file).exists())

    def test_permission_denied_pid_is_considered_alive(self):
        if os.name == "nt":
            self.skipTest("POSIX liveness probe")
        with mock.patch.object(supervisor.os, "kill", side_effect=PermissionError):
            self.assertTrue(supervisor._pid_is_running(123456))

    def test_nonpositive_pid_never_probes_process_group(self):
        with mock.patch.object(supervisor.os, "kill") as probe:
            self.assertFalse(supervisor._pid_is_running(0))
            self.assertFalse(supervisor._pid_is_running(-1))
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
