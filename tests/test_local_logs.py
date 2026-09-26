"""Local synthetic log tests; no worker, Redis, or application database imports."""

import ast
import io
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import List
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import local_logs
from local_logs import TAIL_READ_BYTES, compact_inactive_log, tail_lines


class RecordingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class TailLinesTests(unittest.TestCase):
    def test_filters_blank_lines_and_keeps_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            path.write_text("older\n\n  first  \n \nlast\n", encoding="utf-8")
            self.assertEqual(tail_lines(path, 2), ["  first", "last"])

    def test_large_file_reads_only_fixed_tail(self):
        # Instrument the actual read calls: a short result alone would not
        # catch the former whole-file read followed by a slice.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.log"
            with path.open("wb") as stream:
                stream.seek(64 * 1024 * 1024)
                stream.write(b"\nnewest\n")
            actual_open = Path.open
            read_sizes = []

            class ReadRecorder:
                def __enter__(self):
                    self.stream = actual_open(path, "rb")
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def seek(self, *args):
                    return self.stream.seek(*args)

                def read(self, size=-1):
                    read_sizes.append(size)
                    return self.stream.read(size)

            with mock.patch.object(Path, "open", return_value=ReadRecorder()):
                self.assertEqual(tail_lines(path, 1), ["newest"])
            self.assertEqual(read_sizes, [TAIL_READ_BYTES])

    def test_utf8_byte_window_does_not_invent_replacement_characters(self):
        stream = RecordingStream("界".encode("utf-8") + b"x" * (TAIL_READ_BYTES - 2))
        with mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(tail_lines(Path("unused"), 1), ["x" * (TAIL_READ_BYTES - 2)])
        self.assertEqual(stream.read_sizes, [TAIL_READ_BYTES])

    def test_incomplete_last_utf8_write_is_omitted(self):
        stream = RecordingStream("完成".encode("utf-8") + b"\nnext " + b"\xe7\x95")
        with mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(tail_lines(Path("unused")), ["完成", "next"])

    def test_invalid_bytes_inside_log_are_replaced(self):
        stream = RecordingStream(b"before\xffafter\n")
        with mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(tail_lines(Path("unused")), ["before\ufffdafter"])

    def test_missing_empty_and_nonpositive_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.log"
            self.assertEqual(tail_lines(path), [])
            path.touch()
            self.assertEqual(tail_lines(path), [])
        with mock.patch.object(Path, "open") as opener:
            self.assertEqual(tail_lines(Path("unused"), 0), [])
            self.assertEqual(tail_lines(Path("unused"), -2), [])
            opener.assert_not_called()

    def test_concurrent_truncation_retries_once_with_bounded_reads(self):
        class TruncatingStream(RecordingStream):
            def read(self, size=-1):
                if not self.read_sizes:
                    old_offset = self.tell()
                    self.seek(0)
                    self.truncate()
                    self.write(b"replacement\n")
                    self.seek(old_offset)
                return super().read(size)

        stream = TruncatingStream(b"old\n" * TAIL_READ_BYTES)
        with mock.patch.object(Path, "open", return_value=stream):
            self.assertEqual(tail_lines(Path("unused")), ["replacement"])
        self.assertEqual(stream.read_sizes, [TAIL_READ_BYTES, len(b"replacement\n")])


def load_log_launcher(log_directory):
    """Extract only the generic subprocess wrapper, never import the controller."""
    path = PROJECT_ROOT / "scripts" / "tps_control.py"
    parsed = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(item for item in parsed.body if isinstance(item, ast.FunctionDef) and item.name == "_start_process")
    namespace = {
        "LOG_DIR": log_directory,
        "SCRIPTS": log_directory,
        "Path": Path,
        "List": List,
        "compact_inactive_log": compact_inactive_log,
        "os": os,
        "stat": stat,
        "subprocess": subprocess,
        "time": time,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_start_process"]


class LogLauncherTests(unittest.TestCase):
    def test_new_and_existing_logs_are_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            log_directory = Path(directory)
            launch = load_log_launcher(log_directory)
            existing = log_directory / "existing.log"
            existing.write_bytes(b"synthetic old line\n")
            existing.chmod(0o644)
            with mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=73)):
                launch(["unused"], "new.log")
                launch(["unused"], "existing.log")
            self.assertEqual((log_directory / "new.log").stat().st_mode & 0o777, 0o600)
            self.assertEqual(existing.stat().st_mode & 0o777, 0o600)

    def test_symlinked_log_file_and_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            actual = base / "actual"
            actual.mkdir()
            target = base / "target.txt"
            target.write_bytes(b"do not change")
            (actual / "link.log").symlink_to(target)
            launch = load_log_launcher(actual)
            with mock.patch.object(subprocess, "Popen") as popen:
                with self.assertRaises(ValueError):
                    launch(["unused"], "link.log")
            self.assertEqual(target.read_bytes(), b"do not change")
            popen.assert_not_called()

            linked_directory = base / "logs-link"
            linked_directory.symlink_to(actual, target_is_directory=True)
            launch_linked = load_log_launcher(linked_directory)
            with mock.patch.object(subprocess, "Popen") as popen:
                with self.assertRaises(ValueError):
                    launch_linked(["unused"], "other.log")
            self.assertFalse((actual / "other.log").exists())
            popen.assert_not_called()

    def test_log_name_cannot_escape_log_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            launch = load_log_launcher(Path(directory) / "logs")
            with self.assertRaises(ValueError):
                launch(["unused"], "../escape.log")
            self.assertFalse((Path(directory) / "logs").exists())

    def test_parent_handle_closes_while_child_retains_output(self):
        with tempfile.TemporaryDirectory() as directory:
            log_directory = Path(directory)
            launch = load_log_launcher(log_directory)
            actual_popen = subprocess.Popen
            calls = []

            def capture(*args, **kwargs):
                self.assertFalse(kwargs["stdout"].closed)
                child = actual_popen(*args, **kwargs)
                calls.append((child, kwargs["stdout"]))
                return child

            with mock.patch.object(subprocess, "Popen", side_effect=capture):
                pid = launch([sys.executable, "-c", "import time; time.sleep(0.03); print('synthetic child output')"], "child.log")
            child, parent_handle = calls[0]
            try:
                self.assertEqual(pid, child.pid)
                self.assertTrue(parent_handle.closed)
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertIn("synthetic child output", (log_directory / "child.log").read_text())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_launch_failure_closes_parent_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            launch = load_log_launcher(Path(directory))
            handles = []

            def fail(*args, **kwargs):
                handles.append(kwargs["stdout"])
                raise OSError("synthetic launch failure")

            with mock.patch.object(subprocess, "Popen", side_effect=fail):
                with self.assertRaises(OSError):
                    launch(["unused"], "failure.log")
            self.assertTrue(handles[0].closed)


class LogCompactionTests(unittest.TestCase):
    def test_compaction_does_not_leave_a_partial_utf8_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            path.write_bytes(b"old" * 128 + "界".encode("utf-8") + b"x" * 126)
            with mock.patch.object(local_logs, "_no_open_handles", return_value=True):
                self.assertTrue(compact_inactive_log(path, 256, 128))
            self.assertEqual(path.read_text(encoding="utf-8"), "x" * 126)

    def test_retains_tail_and_permissions_without_leaving_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            path.write_bytes(b"old text\n" * 128 + b"latest content\n")
            path.chmod(0o600)
            expected = path.read_bytes()[-128:]
            with mock.patch.object(local_logs, "_no_open_handles", return_value=True) as probe:
                self.assertTrue(compact_inactive_log(path, maximum_bytes=256, retained_bytes=128))
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_missing_and_small_logs_do_not_probe_or_create_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            with mock.patch.object(local_logs, "_no_open_handles") as probe:
                self.assertFalse(compact_inactive_log(path, 256, 128))
                self.assertFalse(path.exists())
                path.write_bytes(b"small")
                self.assertFalse(compact_inactive_log(path, 256, 128))
                probe.assert_not_called()
            self.assertEqual(path.read_bytes(), b"small")

    def test_active_or_inconclusive_logs_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            original = b"active log\n" * 128
            path.write_bytes(original)
            with mock.patch.object(local_logs, "_no_open_handles", return_value=False):
                self.assertFalse(compact_inactive_log(path, 256, 128))
            self.assertEqual(path.read_bytes(), original)

    def test_writer_appearing_before_replace_skips_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            original = b"old log\n" * 128
            path.write_bytes(original)
            with mock.patch.object(local_logs, "_no_open_handles", side_effect=[True, False]):
                self.assertFalse(compact_inactive_log(path, 256, 128))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_file_changed_before_replace_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            original = b"old log\n" * 128
            path.write_bytes(original)
            probes = []

            def probe(unused_path):
                probes.append(True)
                if len(probes) == 2:
                    with path.open("ab") as stream:
                        stream.write(b"new write\n")
                return True

            with mock.patch.object(local_logs, "_no_open_handles", side_effect=probe):
                self.assertFalse(compact_inactive_log(path, 256, 128))
            self.assertEqual(path.read_bytes(), original + b"new write\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_symlinks_are_never_compacted(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.log"
            original = b"unchanged\n" * 128
            target.write_bytes(original)
            path = Path(directory) / "link.log"
            path.symlink_to(target)
            with mock.patch.object(local_logs, "_no_open_handles") as probe:
                self.assertFalse(compact_inactive_log(path, 256, 128))
                probe.assert_not_called()
            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_bytes(), original)

    def test_probe_fails_closed_without_lsof_or_on_warning_timeout(self):
        path = Path("synthetic.log")
        with mock.patch.object(local_logs.shutil, "which", return_value=None):
            self.assertFalse(local_logs._no_open_handles(path))
        with mock.patch.object(local_logs.shutil, "which", return_value="/mock/lsof"):
            for result in [
                subprocess.CompletedProcess([], 0, "p123\n", ""),
                subprocess.CompletedProcess([], 1, "", "permission denied"),
                subprocess.CompletedProcess([], 2, "", ""),
            ]:
                with mock.patch.object(local_logs.subprocess, "run", return_value=result):
                    self.assertFalse(local_logs._no_open_handles(path))
            with mock.patch.object(local_logs.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 2)):
                self.assertFalse(local_logs._no_open_handles(path))
            with mock.patch.object(local_logs.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
                self.assertTrue(local_logs._no_open_handles(path))

    def test_failed_atomic_replace_preserves_original_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.log"
            original = b"old log\n" * 128
            path.write_bytes(original)
            with mock.patch.object(local_logs, "_no_open_handles", return_value=True):
                with mock.patch.object(local_logs.os, "replace", side_effect=OSError("synthetic failure")):
                    self.assertFalse(compact_inactive_log(path, 256, 128))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
