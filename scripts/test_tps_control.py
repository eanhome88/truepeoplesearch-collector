#!/usr/bin/env python3
import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import multi_worker_runner
import bulk_ingester_daemon
import protocol_worker
import tps_control

from tps_control import (
    _is_discover_cmd,
    _is_worker_cmd,
    normalize_letters,
)


class TestTpsControl(unittest.TestCase):
    def test_normalize_letters(self):
        self.assertEqual(normalize_letters("A"), "a")
        self.assertEqual(normalize_letters("a-c"), "a-c")
        self.assertEqual(normalize_letters("all"), "all")
        with self.assertRaises(ValueError):
            normalize_letters("1")

    def test_cmd_match(self):
        self.assertTrue(_is_worker_cmd("python3 distributed_worker.py --mode worker"))
        self.assertTrue(_is_worker_cmd("python3 distributed_worker.py --concurrency 5"))
        self.assertFalse(_is_worker_cmd("python3 distributed_worker.py --mode feed --file x"))
        self.assertTrue(_is_discover_cmd("python3 discover.py --letters a"))
        self.assertFalse(_is_discover_cmd("python3 test_discover.py"))

    def test_only_this_checkout_script_is_discovered(self):
        own = str(tps_control.SCRIPTS / "distributed_worker.py")
        with mock.patch.object(tps_control, "_list_ps", return_value=[
            {"pid": 10, "cmd": "python3 /other/project/scripts/distributed_worker.py --mode worker"},
            {"pid": 11, "cmd": f"python3 -u {own} --mode worker"},
        ]), mock.patch.object(tps_control, "pid_alive", return_value=True):
            self.assertEqual([row["pid"] for row in tps_control.find_role_pids("worker")], [11])

    def test_saved_pid_reuse_is_not_signalled(self):
        record = {"pid": 42, "identity": {
            "pid": 42, "create_time": 100.0,
            "script": str(tps_control.SCRIPTS / "multi_worker_runner.py"),
            "session_id": 42, "group_id": 42,
        }}

        class FakeRedis:
            def get(self, _key):
                return json.dumps(record)

            def delete(self, _key):
                raise AssertionError("mismatched record must remain available for inspection")

        with mock.patch.object(tps_control, "_process_identity", return_value=(object(), {"create_time": 101.0})), \
                mock.patch.object(tps_control, "pid_alive", return_value=True), \
                mock.patch.object(tps_control, "_stop_owned_tree") as stop_tree:
            result = tps_control._stop_control(FakeRedis(), tps_control.CONTROL_CLUSTER_KEY,
                                               "cluster", "multi_worker_runner.py", lambda: [], manager=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["still_running"], [42])
        stop_tree.assert_not_called()

    def test_missing_psutil_never_falls_back_to_pid_signal(self):
        with mock.patch.object(tps_control, "psutil", None), \
                mock.patch.object(tps_control, "_stop_owned_tree") as stop_tree:
            result = tps_control._stop_control(object(), "key", "worker", "distributed_worker.py", lambda: [])
            with self.assertRaises(RuntimeError):
                tps_control.start_cluster(object())
        self.assertFalse(result["ok"])
        stop_tree.assert_not_called()

    def test_session_and_script_must_match_at_capture(self):
        class FakeProcess:
            def cmdline(self):
                return ["python3", "-u", str(tps_control.SCRIPTS / "multi_worker_runner.py")]

            def create_time(self):
                return 123.456

        with mock.patch.object(tps_control.psutil, "Process", return_value=FakeProcess()), \
                mock.patch.object(tps_control.os, "getsid", return_value=42), \
                mock.patch.object(tps_control.os, "getpgid", return_value=42):
            self.assertEqual(tps_control._process_identity(42, "multi_worker_runner.py")[1]["create_time"], 123.456)
        with mock.patch.object(tps_control.psutil, "Process", return_value=FakeProcess()), \
                mock.patch.object(tps_control.os, "getsid", return_value=99), \
                mock.patch.object(tps_control.os, "getpgid", return_value=42):
            self.assertIsNone(tps_control._process_identity(42, "multi_worker_runner.py"))

    def test_manager_receives_one_term_and_child_gets_no_duplicate(self):
        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
                self.alive = True
                self.signals = []
                self.descendants = []

            def children(self, recursive=False):
                return self.descendants

            def create_time(self):
                return float(self.pid)

            def is_running(self):
                return self.alive

            def status(self):
                return "running"

            def send_signal(self, sig):
                self.signals.append(sig)
                self.alive = False
                for child in self.descendants:
                    child.alive = False

            def wait(self, timeout=None):
                return 0

        root = FakeProcess(42)
        child = FakeProcess(43)
        root.descendants = [child]
        self.assertEqual(tps_control._stop_owned_tree(root, manager=True), [])
        self.assertEqual(root.signals, [signal.SIGTERM])
        self.assertEqual(child.signals, [])

        root.alive = True
        child.alive = True
        root.signals.clear()
        self.assertEqual(tps_control._stop_owned_tree(root, manager=False), [])
        self.assertEqual(root.signals, [signal.SIGTERM])
        self.assertEqual(child.signals, [])

    def test_failed_registration_terminates_just_launched_process(self):
        guard = mock.Mock()
        launched = mock.Mock(pid=73)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(tps_control, "LOG_DIR", Path(directory)), \
                mock.patch.object(tps_control, "compact_inactive_log"), \
                mock.patch.object(tps_control.subprocess, "Popen", return_value=launched), \
                mock.patch.object(tps_control.psutil, "Process", return_value=guard), \
                mock.patch.object(tps_control, "_stop_owned_tree", return_value=[]) as stop_tree:
            with self.assertRaisesRegex(RuntimeError, "record failed"):
                tps_control._start_process(
                    ["python3", "synthetic.py"], "synthetic.log",
                    lambda _pid: (_ for _ in ()).throw(RuntimeError("record failed")),
                )
        stop_tree.assert_called_once_with(guard, manager=False)

    def test_failed_manager_registration_captures_and_kills_unresponsive_descendant(self):
        class FakeProcess:
            def __init__(self, pid, descendants=()):
                self.pid = pid
                self.descendants = list(descendants)
                self.alive = True
                self.signals = []

            def children(self, recursive=False):
                return self.descendants

            def create_time(self):
                return float(self.pid)

            def is_running(self):
                return self.alive

            def status(self):
                return "running"

            def send_signal(self, sig):
                self.signals.append(sig)
                self.alive = False

            def wait(self, timeout=None):
                return 0

        child = FakeProcess(81)
        manager = FakeProcess(80, [child])
        launched = mock.Mock(pid=80)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(tps_control, "LOG_DIR", Path(directory)), \
                mock.patch.object(tps_control, "compact_inactive_log"), \
                mock.patch.object(tps_control.subprocess, "Popen", return_value=launched), \
                mock.patch.object(tps_control.psutil, "Process", return_value=manager), \
                mock.patch.object(tps_control.time, "monotonic", side_effect=[0, 19, 19, 19.1, 19.2]):
            with self.assertRaisesRegex(RuntimeError, "record failed"):
                tps_control._start_process(
                    ["python3", str(tps_control.SCRIPTS / "multi_worker_runner.py")],
                    "cluster.log", lambda _pid: (_ for _ in ()).throw(RuntimeError("record failed")),
                )
        self.assertEqual(manager.signals, [signal.SIGTERM])
        self.assertEqual(child.signals, [signal.SIGKILL])

    def test_failed_process_lookup_uses_only_new_popen_handle(self):
        launched = mock.Mock(pid=74)
        launched.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(tps_control, "LOG_DIR", Path(directory)), \
                mock.patch.object(tps_control, "compact_inactive_log"), \
                mock.patch.object(tps_control.subprocess, "Popen", return_value=launched), \
                mock.patch.object(tps_control.psutil, "Process", side_effect=tps_control.psutil.AccessDenied(pid=74)):
            with self.assertRaises(tps_control.psutil.AccessDenied):
                tps_control._start_process(["python3", "synthetic.py"], "synthetic.log", lambda _pid: None)
        launched.terminate.assert_called_once()
        launched.wait.assert_called_once_with(timeout=2)

    def test_manager_repeated_term_is_idempotent(self):
        manager = multi_worker_runner.MultiWorkerManager(1, 10)
        child = mock.Mock()
        child.poll.return_value = None
        manager.processes = [child]
        with mock.patch.object(multi_worker_runner.os, "_exit") as emergency_exit:
            manager._handle_signal(signal.SIGTERM, None)
            manager._handle_signal(signal.SIGTERM, None)
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
        emergency_exit.assert_not_called()

    def test_manager_cleans_captured_children_when_second_worker_launch_fails(self):
        class FakePopen:
            def __init__(self, pid):
                self.pid = pid
                self.alive = True
                self.terms = 0
                self.kills = 0

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.terms += 1
                self.alive = False

            def kill(self):
                self.kills += 1
                self.alive = False

            def wait(self, timeout=None):
                return 0

        ingester = FakePopen(71)
        first_worker = FakePopen(72)
        manager = multi_worker_runner.MultiWorkerManager(2, 10)
        with mock.patch.object(multi_worker_runner.signal, "signal"), \
                mock.patch.object(multi_worker_runner.time, "sleep"), \
                mock.patch.object(manager, "_wait_for_ingester_ready"), \
                mock.patch.object(multi_worker_runner.subprocess, "Popen",
                                  side_effect=[ingester, first_worker, OSError("synthetic second launch failure")]):
            with self.assertRaisesRegex(OSError, "second launch failure"):
                manager.start()
        self.assertEqual([p.pid for p in manager._managed_processes()], [72, 71])
        self.assertEqual((first_worker.terms, ingester.terms), (1, 1))
        self.assertEqual((first_worker.kills, ingester.kills), (0, 0))

    def test_manager_cli_keeps_proxy_file_and_rejects_secret_arguments(self):
        with mock.patch.dict(os.environ, {"TPS_ALLOW_CLUSTER": "1"}), \
                mock.patch.object(sys, "argv", ["multi_worker_runner.py", "--proxy-file", "/tmp/proxies.txt"]), \
                mock.patch.object(multi_worker_runner, "MultiWorkerManager") as manager_type:
            multi_worker_runner.main()
        self.assertEqual(manager_type.call_args.kwargs["proxy_file"], "/tmp/proxies.txt")

        secret = "http://user:synthetic-secret@provider.invalid"
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["multi_worker_runner.py", "--proxy-tunnel", secret]), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                multi_worker_runner.main()
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("PROXY_TUNNEL", stderr.getvalue())

    def test_worker_and_ingester_ignore_repeated_term(self):
        for module, worker_type in ((protocol_worker, protocol_worker.ProtocolWorker),
                                    (bulk_ingester_daemon, bulk_ingester_daemon.BulkIngesterDaemon)):
            worker = object.__new__(worker_type)
            worker.stopping = False
            with mock.patch.object(module.os, "_exit") as emergency_exit:
                args = (signal.SIGTERM,) if module is protocol_worker else (signal.SIGTERM, None)
                worker._handle_signal(*args)
                worker._handle_signal(*args)
            self.assertTrue(worker.stopping)
            emergency_exit.assert_not_called()

    def test_cluster_proxy_secret_is_not_in_argv_or_status(self):
        secret = "http://user:private-password@local-proxy:8080"
        captured = {}

        class FakeRedis:
            def set(self, key, value, **options):
                if options.get("nx"):
                    captured["lock"] = (key, value)
                    return True
                captured["key"] = key
                captured["value"] = value
                return True

            def get(self, _key):
                return None

            def eval(self, _script, numkeys, *values):
                if numkeys == 2:
                    captured["key"] = values[1]
                    captured["value"] = values[3]
                return 1

        def fake_launch(cmd, _log, register, env):
            captured["cmd"] = cmd
            captured["env"] = env
            register(42)
            return 42

        identity = {"pid": 42, "create_time": 123.456,
                    "script": str(tps_control.SCRIPTS / "multi_worker_runner.py"),
                    "session_id": 42, "group_id": 42}
        with mock.patch.object(tps_control, "find_cluster_pids", return_value=[]), \
                mock.patch.object(tps_control, "_start_process", side_effect=fake_launch), \
                mock.patch.object(tps_control, "_process_identity", return_value=(object(), identity)):
            tps_control.start_cluster(FakeRedis(), proxy_tunnel=secret)
        self.assertNotIn(secret, " ".join(captured["cmd"]))
        self.assertEqual(captured["env"]["PROXY_TUNNEL"], secret)
        self.assertNotIn(secret, captured["value"])
        manager = multi_worker_runner.MultiWorkerManager(1, 10, proxy_tunnel=secret)
        self.assertNotIn(secret, " ".join(manager._build_worker_cmd(0)))
        self.assertEqual(manager._worker_env()["PROXY_TUNNEL"], secret)

    def test_start_lock_serializes_same_role_and_releases_only_own_token(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}

            def set(self, key, value, nx=False, ex=None):
                if nx and key in self.values:
                    return False
                self.values[key] = value
                return True

            def eval(self, script, numkeys, *values):
                key, token = values[:2]
                if self.values.get(key) != token:
                    return 0
                if numkeys == 1 and "DEL" in script:
                    del self.values[key]
                return 1

        redis = FakeRedis()
        lock_key = f"{tps_control.CONTROL_WORKER_KEY}:start-lock"

        @tps_control._serialized_start(tps_control.CONTROL_WORKER_KEY)
        def nested_start(r):
            second = tps_control._serialized_start(tps_control.CONTROL_WORKER_KEY)(lambda _r: {"ok": True})(r)
            self.assertFalse(second["ok"])
            self.assertEqual(second["error"], "正在执行控制操作，请稍后重试")
            self.assertIn(lock_key, r.values)
            return {"ok": True}

        self.assertTrue(nested_start(redis)["ok"])
        self.assertNotIn(lock_key, redis.values)

        @tps_control._serialized_start(tps_control.CONTROL_WORKER_KEY)
        def lose_lock(r):
            r.values[lock_key] = "another-owner"
            return {"ok": True}

        lose_lock(redis)
        self.assertEqual(redis.values[lock_key], "another-owner")

    def test_start_refuses_when_redis_lock_is_unavailable(self):
        class BrokenRedis:
            def set(self, *_args, **_kwargs):
                raise OSError("synthetic Redis outage")

        with mock.patch.object(tps_control, "_start_process") as launch:
            result = tps_control.start_cluster(BrokenRedis())
        self.assertFalse(result["ok"])
        launch.assert_not_called()

    def test_stop_rejects_during_same_role_start_lock(self):
        class BusyRedis:
            def __init__(self):
                self.keys = []

            def set(self, _key, _value, **options):
                assert options.get("nx") is True
                self.keys.append(_key)
                return False

        redis = BusyRedis()
        with mock.patch.object(tps_control, "_stop_control") as stop_core:
            for stop in (tps_control.stop_worker, tps_control.stop_discover, tps_control.stop_cluster):
                result = stop(redis)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "正在执行控制操作，请稍后重试")
        self.assertEqual(redis.keys, [
            f"{tps_control.CONTROL_WORKER_KEY}:start-lock",
            f"{tps_control.CONTROL_DISCOVER_KEY}:start-lock",
            f"{tps_control.CONTROL_CLUSTER_KEY}:start-lock",
        ])
        stop_core.assert_not_called()

    def test_lost_start_lock_cannot_register_a_new_process(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}

            def set(self, key, value, nx=False, ex=None):
                if nx and key in self.values:
                    return False
                self.values[key] = value
                return True

            def eval(self, _script, numkeys, *values):
                if numkeys == 2:
                    lock_key, control_key, token, payload = values
                    if self.values.get(lock_key) != token:
                        return 0
                    self.values[control_key] = payload
                    return 1
                lock_key, token = values[:2]
                if self.values.get(lock_key) != token:
                    return 0
                if "DEL" in _script:
                    del self.values[lock_key]
                return 1

        redis = FakeRedis()
        control_key = tps_control.CONTROL_CLUSTER_KEY
        lock_key = f"{control_key}:start-lock"
        identity = {"pid": 42, "create_time": 123.456,
                    "script": str(tps_control.SCRIPTS / "multi_worker_runner.py"),
                    "session_id": 42, "group_id": 42}

        @tps_control._serialized_start(control_key)
        def launch(r):
            r.values[lock_key] = "new-owner"
            with mock.patch.object(tps_control, "_process_identity", return_value=(object(), identity)):
                with self.assertRaisesRegex(RuntimeError, "启动锁已失效"):
                    tps_control._record_started_process(r, control_key, 42, "multi_worker_runner.py", {})
            return {"ok": True}

        self.assertTrue(launch(redis)["ok"])
        self.assertNotIn(control_key, redis.values)
        self.assertEqual(redis.values[lock_key], "new-owner")

    def test_old_cluster_status_masks_saved_proxy_secret(self):
        secret = "http://user:private-password@local-proxy:8080"

        class FakeRedis:
            def get(self, _key):
                return json.dumps({"args": {"proxy_tunnel": secret}})

            def llen(self, _key):
                return 0

        with mock.patch.object(tps_control, "find_cluster_pids", return_value=[]), \
                mock.patch.object(tps_control, "_scan_heartbeats", return_value=[]), \
                mock.patch.object(tps_control, "_tail_log", return_value=[f"代理模式配置: {secret}"]):
            status = tps_control.cluster_status(FakeRedis())
        self.assertNotIn(secret, json.dumps(status))

    def test_old_cluster_log_hides_api_url_query_token(self):
        secret = "synthetic-query-token-never-show"
        line = f"[PROXY] 使用 API 动态提取: https://provider.invalid/get?token={secret}"
        self.assertNotIn(secret, tps_control._safe_cluster_log_line(line))

        class FakeRedis:
            def get(self, _key):
                return None

            def llen(self, _key):
                return 0

        with mock.patch.object(tps_control, "find_cluster_pids", return_value=[]), \
                mock.patch.object(tps_control, "_scan_heartbeats", return_value=[]), \
                mock.patch.object(tps_control, "_tail_log", return_value=[line]):
            status = tps_control.cluster_status(FakeRedis())
        self.assertNotIn(secret, json.dumps(status))

    def test_pipeline_log_previews_hide_old_proxy_secrets(self):
        token = "synthetic-token-never-show"
        old_lines = [
            f"代理模式配置: user:{token}@proxy.invalid:8080",
            f"[PROXY] https://provider.invalid/get?token={token}",
        ]

        class FakeRedis:
            def llen(self, _key):
                return 0

            def scard(self, _key):
                return 0

        with mock.patch.object(tps_control, "queue_stats", return_value={}), \
                mock.patch.object(tps_control, "_role_status", return_value={}), \
                mock.patch.object(tps_control, "cluster_status", return_value={}), \
                mock.patch.object(tps_control, "peek_processing", return_value=[]), \
                mock.patch.object(tps_control, "peek_dlq", return_value=[]), \
                mock.patch.object(tps_control, "_tail_log", return_value=old_lines):
            result = tps_control.pipeline_status(FakeRedis())
        for role in ("worker", "discover", "cluster"):
            preview = json.dumps(result["logs"][role], ensure_ascii=False)
            self.assertNotIn(token, preview)
            self.assertIn("已配置", preview)
            self.assertIn("[URL 已隐藏]", preview)


if __name__ == "__main__":
    unittest.main(verbosity=2)
