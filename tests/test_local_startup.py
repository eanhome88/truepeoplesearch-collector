"""Offline launcher tests. All Python-service and Docker CLI calls are fake."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "start.sh"
FAKE_CLI = r'''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
state = Path(os.environ["FAKE_STATE"])
if name == "fake-python":
    if args[0] == "-c":
        if "# local-startup-docker-wrapper" in args[1]:
            import subprocess
            if os.environ.get("FAKE_DOCKER_TIMEOUT"):
                def timeout(*a, **kw):
                    raise subprocess.TimeoutExpired(a[0], kw["timeout"], stderr=b"connection to https://name:secret@invalid timed out\n")
                subprocess.run = timeout
            sys.argv = ["-c", *args[2:]]
            exec(compile(args[1], "docker-wrapper", "exec"), {})
            sys.exit(0)
        if os.environ.get("FAKE_DEP_FAIL"):
            print("fake dependency import failed", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    if args[0] == "-":
        sys.stdin.read()
        service = args[-1]
        mode = os.environ.get("FAKE_" + service.upper(), "ready")
        if mode == "down" or (mode == "stopped" and not (state / service).exists()):
            print("fake " + service + " unavailable", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    sys.exit(int(os.environ.get("FAKE_APP_EXIT", "0")))
if name == "docker":
    if args[:2] == ["context", "inspect"]:
        print(os.environ.get("FAKE_DOCKER_ENDPOINT", "unix:///fake/docker.sock"))
        sys.exit(0)
    if args[0] == "start":
        if os.environ.get("FAKE_START_FAIL"):
            print("fake Docker permission denied", file=sys.stderr)
            sys.exit(1)
        (state / args[1]).touch()
        print(args[1])
        sys.exit(0)
    if args[:2] == ["container", "inspect"]:
        if os.environ.get("FAKE_NO_CONTAINER"):
            print(os.environ.get("FAKE_DOCKER_ERROR", "fake no such container"), file=sys.stderr)
            sys.exit(1)
        fmt = args[3]
        if fmt == "{{.State.Running}}":
            print("true" if (state / args[-1]).exists() else "false")
        elif fmt == "{{.Config.Image}}":
            print(os.environ.get("FAKE_IMAGE", "example/service:latest"))
        elif ".Mounts" in fmt:
            print(os.environ.get("FAKE_MOUNTS", "volume | fixture-data | /fixture"))
        else:
            sys.exit(98)
        sys.exit(0)
    print("forbidden fake Docker action", file=sys.stderr)
    sys.exit(99)
sys.exit(97)
'''


class LocalStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="local-startup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.calls_file = self.directory / "calls.jsonl"
        self.state = self.directory / "state"
        self.state.mkdir()
        for name in ("fake-python", "docker"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
            path.chmod(0o755)
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("TPS_", "FAKE_", "DOCKER_"))
        }
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin",
            "TPS_PYTHON": str(self.bin / "fake-python"),
            "FAKE_CALLS": str(self.calls_file),
            "FAKE_STATE": str(self.state),
        })

    def run_start(self, *args, **env):
        result = subprocess.run(
            ["/bin/bash", str(START), *args], env=dict(self.env, **env),
            capture_output=True, text=True, timeout=12,
        )
        self.calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()] if self.calls_file.exists() else []
        for call in self.calls:
            if call[0] == "docker":
                self.assertIn(call[1], ("container", "context", "start"), call)
        return result

    def test_check_is_read_only_and_does_not_launch_app(self):
        result = self.run_start("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("只读检查通过", result.stdout)
        self.assertFalse(any(call[1] == "start" for call in self.calls))
        self.assertFalse(any("dashboard_api.py" in call[1] for call in self.calls))
        self.assertIn("fixture-data", result.stdout)
        self.assertIn("latest 未锁定版本", result.stdout)

    def test_check_failure_never_restores_stopped_container(self):
        result = self.run_start("--check", FAKE_TIDB="stopped")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[1] == "start" for call in self.calls))
        self.assertNotIn("只读检查通过", result.stdout)

    def test_missing_dependencies_fail_without_service_or_docker_calls(self):
        result = self.run_start(FAKE_DEP_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fake dependency import failed", result.stderr)
        self.assertEqual(len(self.calls), 1)

    def test_stopped_containers_are_restored_without_creation(self):
        result = self.run_start(FAKE_TIDB="stopped", FAKE_REDIS="stopped")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["docker", "start", "tidb"], self.calls)
        self.assertIn(["docker", "start", "redis"], self.calls)
        self.assertEqual(self.calls[-1], ["fake-python", str(ROOT / "tools/dashboard_api.py"), "--host", "127.0.0.1", "--port", "5001"])
        self.assertNotIn("系统就绪", result.stdout)

    def test_missing_container_does_not_create_or_pull(self):
        result = self.run_start(FAKE_TIDB="down", FAKE_NO_CONTAINER="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fake no such container", result.stderr)
        self.assertFalse(any(call[1] == "start" for call in self.calls))
        self.assertIn("不会创建容器或下载镜像", result.stderr)

    def test_docker_start_error_is_visible(self):
        result = self.run_start(FAKE_TIDB="stopped", FAKE_START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fake Docker permission denied", result.stderr)

    def test_readiness_timeout_fails_without_launching_app(self):
        result = self.run_start("--timeout", "1", FAKE_TIDB="down")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("等待 tidb 就绪超时", result.stderr)
        self.assertFalse(any("dashboard_api.py" in call[1] for call in self.calls))

    def test_remote_service_failure_does_not_start_local_container(self):
        result = self.run_start(FAKE_TIDB="down", TPS_DB_HOST="db.example.invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("非本机地址", result.stderr)
        self.assertNotIn(["docker", "start", "tidb"], self.calls)

    def test_app_exit_code_and_address_are_preserved(self):
        result = self.run_start(FAKE_APP_EXIT="23", TPS_DASHBOARD_PORT="5055")
        self.assertEqual(result.returncode, 23)
        self.assertEqual(self.calls[-1][-1], "5055")

    def test_remote_docker_context_is_never_started(self):
        result = self.run_start(FAKE_TIDB="stopped", FAKE_DOCKER_ENDPOINT="ssh://remote.invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("非本机套接字上下文", result.stderr)
        self.assertNotIn(["docker", "start", "tidb"], self.calls)

    def test_remote_docker_host_override_is_never_started(self):
        result = self.run_start(FAKE_TIDB="stopped", DOCKER_HOST="tcp://remote.invalid:2375")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("非本机套接字上下文", result.stderr)
        self.assertNotIn(["docker", "start", "tidb"], self.calls)

    def test_missing_mounts_are_reported_without_claiming_data_loss(self):
        result = self.run_start("--check", FAKE_MOUNTS="", FAKE_IMAGE="example/service@sha256:fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("未发现挂载", result.stdout)
        self.assertIn("不表示已发生数据丢失", result.stdout)
        self.assertNotIn("latest 未锁定版本", result.stdout)

    def test_invalid_timeout_fails_before_any_external_call(self):
        for timeout in ("0", "301", "garbage"):
            with self.subTest(timeout=timeout):
                result = self.run_start("--timeout", timeout)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls, [])

    def test_docker_error_redacts_url_credentials(self):
        result = self.run_start("--check", FAKE_NO_CONTAINER="1", FAKE_DOCKER_ERROR="cannot connect https://admin:secret-password@example.invalid/daemon")
        self.assertEqual(result.returncode, 0)
        self.assertIn("https://[redacted]@example.invalid", result.stderr)
        self.assertNotIn("secret-password", result.stderr + result.stdout)

    def test_docker_timeout_is_reported_without_credentials(self):
        result = self.run_start(FAKE_TIDB="stopped", FAKE_DOCKER_TIMEOUT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker 命令超时（10 秒）", result.stderr)
        self.assertNotIn("name:secret", result.stderr + result.stdout)
        self.assertFalse(any(call[:2] == ["docker", "start"] for call in self.calls))


class EmbeddedProbeTests(unittest.TestCase):
    def run_probe(self, missing_table=False, old_driver=False, service="tidb", redis_ready=True):
        source = START.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

        class DatabaseError(Exception):
            errno = 1146

        class ReadinessDriverUnsupported(Exception):
            code = "mysql_connector_9_2_required"

        dashboard = types.ModuleType("dashboard_api")
        dashboard.check_local_database_ready = mock.Mock(return_value=True)
        dashboard._redis_ready = mock.Mock(return_value=redis_ready)
        if missing_table:
            dashboard.check_local_database_ready.side_effect = DatabaseError("secret-password-must-not-leak")
        if old_driver:
            dashboard.check_local_database_ready.side_effect = ReadinessDriverUnsupported("secret-password-must-not-leak")
        error = io.StringIO()
        with mock.patch.dict(sys.modules, {"dashboard_api": dashboard}), \
                mock.patch.object(sys, "argv", ["-", "fake-tools", service]), \
                mock.patch.object(sys, "path", list(sys.path)), contextlib.redirect_stderr(error):
            if missing_table or old_driver or not redis_ready:
                with self.assertRaises(SystemExit) as raised:
                    exec(compile(source, "startup-probe", "exec"), {})
                self.assertEqual(raised.exception.code, 1)
            else:
                exec(compile(source, "startup-probe", "exec"), {})
        return dashboard, error.getvalue()

    def test_database_probe_delegates_to_shared_readiness(self):
        dashboard, error = self.run_probe()
        self.assertEqual(error, "")
        dashboard.check_local_database_ready.assert_called_once_with()
        dashboard._redis_ready.assert_not_called()

    def test_missing_schema_is_failure_and_credentials_stay_hidden(self):
        _, error = self.run_probe(missing_table=True)
        self.assertIn("1146", error)
        self.assertNotIn("secret-password", error)

    def test_old_driver_error_is_actionable_without_secrets(self):
        _, error = self.run_probe(old_driver=True)
        self.assertIn("mysql-connector-python >= 9.2", error)
        self.assertNotIn("secret-password", error)

    def test_redis_failure_uses_shared_readiness_and_fails(self):
        dashboard, error = self.run_probe(service="redis", redis_ready=False)
        self.assertIn("redis 未就绪", error)
        dashboard._redis_ready.assert_called_once_with()
        dashboard.check_local_database_ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
