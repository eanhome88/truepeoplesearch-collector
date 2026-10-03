"""Updater regression tests: all subprocess/database/network operations mocked."""

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tps_version
import mysql.connector

_init_spec = importlib.util.spec_from_file_location("init_db_failure_test", ROOT / "deploy" / "init_db.py")
init_db = importlib.util.module_from_spec(_init_spec)
_init_spec.loader.exec_module(init_db)


class UpdateFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="update-failure-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.joinpath("requirements.txt").write_text("# synthetic", encoding="utf-8")
        self.pip = self.root / ".venv/bin/pip"
        self.pip.parent.mkdir(parents=True)
        self.pip.write_text("", encoding="utf-8")
        self.root.joinpath("deploy").mkdir()
        self.root.joinpath("deploy/init_db.py").write_text("# synthetic", encoding="utf-8")
        self.root.joinpath("supervisor.sh").write_text("# synthetic", encoding="utf-8")
        self.outcomes = {}
        self.phases = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(tps_version, "_ROOT_DIR", self.root))
        self.stack.enter_context(mock.patch.object(tps_version, "sys", SimpleNamespace(platform="linux", executable=sys.executable)))
        self.stack.enter_context(mock.patch.object(tps_version.os, "access", return_value=True))
        self.git = self.stack.enter_context(mock.patch.object(tps_version, "get_git_status", return_value={
            "has_git": True, "remote_url": "https://example.invalid/synthetic.git",
            "branch": "main", "dirty": False, "short_commit": "synthetic",
        }))
        self.stack.enter_context(mock.patch.object(tps_version, "read_local_version_info", return_value={"version": "1.2.3"}))
        self.process = self.stack.enter_context(mock.patch.object(tps_version.subprocess, "run", side_effect=self.run_phase))

    def run_phase(self, command, **kwargs):
        if command[:2] == ["git", "pull"]:
            phase = "pull"
        elif command[:2] == ["git", "stash"]:
            phase = "stash"
        elif "install" in command:
            phase = "dependencies"
        elif any(str(part).endswith("init_db.py") for part in command):
            phase = "database"
        elif command[-1] == "restart":
            phase = "restart"
        else:
            raise AssertionError(f"Unexpected external command: {command}")
        self.phases.append(phase)
        outcome = self.outcomes.get(phase, 0)
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(command, outcome, stdout="synthetic output", stderr="synthetic error" if outcome else "")

    def test_success_uses_real_sys_module_and_reports_only_command_completion(self):
        result = tps_version.execute_system_update()
        self.assertTrue(result["ok"])
        self.assertEqual(self.phases, ["pull", "dependencies", "database", "restart"])
        migration = self.process.call_args_list[2].args[0]
        self.assertEqual(migration[0], sys.executable)
        self.assertIn("请检查服务就绪状态", result["message"])
        self.assertNotIn("当前运行版本", "\n".join(result["logs"]))

    def test_pull_failure_never_falls_back_to_unrestricted_pull(self):
        self.outcomes["pull"] = 1
        result = tps_version.execute_system_update()
        self.assertFalse(result["ok"])
        self.assertEqual(self.phases, ["pull"])
        self.assertIn("--ff-only", self.process.call_args.args[0])
        self.assertFalse(result["code_updated"])
        self.assertFalse(result["restart_attempted"])

    def test_dependency_failure_does_not_migrate_or_restart(self):
        self.outcomes["dependencies"] = 1
        result = tps_version.execute_system_update()
        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "dependencies")
        self.assertEqual(self.phases, ["pull", "dependencies"])
        self.assertTrue(result["code_updated"])
        self.assertFalse(result["restart_attempted"])

    def test_missing_pip_fails_closed(self):
        self.pip.unlink()
        result = tps_version.execute_system_update()
        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "dependencies")
        self.assertEqual(self.phases, ["pull"])

    def test_migration_failure_does_not_restart(self):
        self.outcomes["database"] = 1
        result = tps_version.execute_system_update()
        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "database")
        self.assertEqual(self.phases, ["pull", "dependencies", "database"])
        self.assertFalse(result["restart_attempted"])

    def test_migration_exception_does_not_restart(self):
        self.outcomes["database"] = OSError("synthetic execution error")
        result = tps_version.execute_system_update()
        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "database")
        self.assertFalse(result["restart_attempted"])

    def test_restart_failure_is_not_reported_success(self):
        self.outcomes["restart"] = 1
        result = tps_version.execute_system_update()
        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "restart")
        self.assertTrue(result["restart_attempted"])

    def test_timeouts_stop_at_and_name_the_failing_phase(self):
        phases = ["pull", "dependencies", "database", "restart"]
        for index, phase in enumerate(phases):
            with self.subTest(phase=phase):
                self.outcomes = {phase: subprocess.TimeoutExpired("synthetic", 1)}
                self.phases.clear()
                result = tps_version.execute_system_update()
                self.assertFalse(result["ok"])
                self.assertEqual(result["phase"], phase)
                self.assertEqual(self.phases, phases[:index + 1])
                self.assertIn(phase, result["error"])

    def test_dirty_worktree_is_rejected_before_any_external_command(self):
        self.git.return_value["dirty"] = True
        result = tps_version.execute_system_update()
        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "preflight")
        self.assertFalse(result["code_updated"])
        self.process.assert_not_called()

    def test_windows_update_requires_manual_restart_without_foreground_process(self):
        windows_pip = self.root / ".venv/Scripts/pip.exe"
        windows_pip.parent.mkdir(parents=True)
        windows_pip.write_text("", encoding="utf-8")
        with mock.patch.object(tps_version.sys, "platform", "win32"):
            result = tps_version.execute_system_update()
        self.assertTrue(result["ok"])
        self.assertTrue(result["restart_required"])
        self.assertFalse(result["restart_attempted"])
        self.assertFalse(result["service_verified"])
        self.assertEqual(self.phases, ["pull", "dependencies", "database"])

    def test_missing_restart_wrapper_requires_manual_restart(self):
        self.root.joinpath("supervisor.sh").unlink()
        result = tps_version.execute_system_update()
        self.assertTrue(result["restart_required"])
        self.assertFalse(result["restart_attempted"])
        self.assertEqual(self.phases, ["pull", "dependencies", "database"])

    def test_failed_requested_stash_does_not_pull(self):
        self.git.return_value["dirty"] = True
        self.outcomes["stash"] = subprocess.CalledProcessError(1, ["git", "stash"])
        result = tps_version.execute_system_update(force_stash=True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.phases, ["stash"])

    def test_windows_wrapper_delegates_once_and_propagates_failure(self):
        batch = ROOT.joinpath("deploy/update.bat").read_text(encoding="utf-8")
        self.assertEqual(batch.count("execute_system_update("), 1)
        self.assertNotIn("git pull", batch)
        self.assertNotIn("force_stash=True", batch)
        self.assertIn("sys.exit(0 if res.get('ok') else 1)", batch)
        self.assertIn("if errorlevel 1 goto update_failed", batch)
        self.assertIn("exit /b 1", batch)


class MigrationFailureTests(unittest.TestCase):
    COLUMNS = ["first_name", "middle_name", "last_name", "gender", "primary_phone",
               "primary_phone_type", "current_address", "address_duration", "all_phones",
               "wireless_phone_1", "wireless_phone_2", "wireless_phone_3"]

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(mock.patch.object(init_db, "load_env_file"))
        self.stack.enter_context(mock.patch.dict(os.environ, {"TPS_DB_HOST": "synthetic.invalid", "TPS_DB_PORT": "4000", "TPS_DB_NAME": "synthetic"}))
        self.port = self.stack.enter_context(mock.patch.object(init_db, "wait_for_port", return_value=True))
        schema = mock.Mock()
        schema.exists.return_value = True
        schema.read_text.return_value = "CREATE TABLE synthetic_table (id INT);"
        self.stack.enter_context(mock.patch.object(init_db, "_SCHEMA_SQL_FILE", schema))
        self.first = mock.Mock()
        self.second = mock.Mock()
        self.cursor = self.second.cursor.return_value
        self.cursor.rowcount = 0
        self.cursor.fetchall.side_effect = [
            [(name,) for name in self.COLUMNS], [(name,) for name in init_db.REQUIRED_TABLES],
        ]
        self.connect = self.stack.enter_context(mock.patch("mysql.connector.connect", side_effect=[self.first, self.second]))

    def assert_failed(self, **kwargs):
        with self.assertRaises(SystemExit) as exit_status:
            init_db.run_init(**kwargs)
        self.assertEqual(exit_status.exception.code, 1)

    def error_on(self, prefix, errno):
        def execute(statement, *args):
            if statement.strip().startswith(prefix):
                raise mysql.connector.Error(msg="synthetic database failure", errno=errno)
        self.cursor.execute.side_effect = execute

    def test_unavailable_configured_database_is_failure_without_port_switch(self):
        self.port.return_value = False
        self.assert_failed()
        self.port.assert_called_once_with("synthetic.invalid", 4000, timeout_sec=15)
        self.connect.assert_not_called()

    def test_schema_permission_failure_is_not_swallowed(self):
        self.error_on("CREATE TABLE", 1142)
        self.assert_failed()

    def test_existing_table_remains_idempotent(self):
        self.error_on("CREATE TABLE", 1050)
        init_db.run_init()

    def test_column_permission_failure_is_not_swallowed(self):
        self.cursor.fetchall.side_effect = [[], [(name,) for name in init_db.REQUIRED_TABLES]]
        self.error_on("ALTER TABLE", 1142)
        self.assert_failed()

    def test_duplicate_column_remains_idempotent(self):
        self.cursor.fetchall.side_effect = [[], [(name,) for name in init_db.REQUIRED_TABLES]]
        self.error_on("ALTER TABLE", 1060)
        init_db.run_init()

    def test_view_failure_is_not_swallowed(self):
        self.error_on("CREATE OR REPLACE VIEW", 1142)
        self.assert_failed()

    def test_cleanup_permission_failure_is_not_swallowed(self):
        self.error_on("DELETE FROM", 1142)
        self.assert_failed(cleanup_invalid_records=True)

    def test_default_schema_initialization_never_deletes_records(self):
        init_db.run_init()
        statements = [str(call.args[0]).strip() for call in self.cursor.execute.call_args_list]
        self.assertFalse(any(statement.startswith("DELETE FROM") for statement in statements))


if __name__ == "__main__":
    unittest.main()
