"""Synthetic credential-output and local-file tests; never touches the real config."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import proxy_pool


class ProxyConfigSecurityTests(unittest.TestCase):
    class FakeRedis:
        def __init__(self, value=None):
            self.value = value
            self.eval_calls = 0
            self.set_calls = 0

        def get(self, _key):
            return self.value

        def set(self, _key, value):
            self.set_calls += 1
            self.value = value
            return True

        def eval(self, script, keys_count, _key, expected, had_previous, previous):
            self.eval_calls += 1
            self.assert_script_shape(script, keys_count)
            if self.value != expected:
                return 0
            self.value = previous if had_previous else None
            return 1

        @staticmethod
        def assert_script_shape(script, keys_count):
            assert keys_count == 1
            assert "redis.call('GET', KEYS[1]) ~= ARGV[1]" in script

    def test_api_credentials_and_failed_response_body_never_reach_logs(self):
        secret = "synthetic-api-token-never-show"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url):
                return type("Response", (), {"status_code": 401, "text": secret})()

        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            manager = proxy_pool.ProxyManager(api_url=f"https://gateway.invalid/list?token={secret}")
            with patch.object(proxy_pool.httpx, "AsyncClient", return_value=FakeClient()):
                self.assertEqual(asyncio.run(manager.refresh_from_api()), 0)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("HTTP 401", output.getvalue())

    def test_redis_error_does_not_reveal_credential_in_logs(self):
        secret = "synthetic-redis-secret-never-show"

        class FailingRedis:
            def get(self, *_args):
                return None

            def set(self, *_args):
                raise RuntimeError(secret)

            def eval(self, *_args):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", Path(directory) / "config.json"):
                with redirect_stderr(output):
                    with self.assertRaisesRegex(RuntimeError, "代理配置未能完整保存"):
                        proxy_pool.save_proxy_config(FailingRedis(), {"mode": "direct"})
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("RuntimeError", output.getvalue())

    def test_local_replace_failure_restores_previous_redis_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.json"
            destination.write_text("old-local", encoding="utf-8")
            redis = self.FakeRedis("old-redis")
            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", destination):
                with patch.object(proxy_pool.os, "replace", side_effect=OSError("synthetic failure")):
                    with redirect_stderr(io.StringIO()):
                        with self.assertRaisesRegex(RuntimeError, "代理配置未能完整保存"):
                            proxy_pool.save_proxy_config(redis, {"mode": "direct"})
            self.assertEqual(redis.value, "old-redis")
            self.assertEqual(redis.eval_calls, 1)
            self.assertEqual(destination.read_text(encoding="utf-8"), "old-local")
            self.assertEqual(
                sorted(path.name for path in Path(directory).iterdir()),
                [".config.json.lock", "config.json"],
            )

    def test_local_replace_failure_removes_new_redis_key(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.json"
            redis = self.FakeRedis()
            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", destination):
                with patch.object(proxy_pool.os, "replace", side_effect=OSError("synthetic failure")):
                    with redirect_stderr(io.StringIO()):
                        with self.assertRaisesRegex(RuntimeError, "代理配置未能完整保存"):
                            proxy_pool.save_proxy_config(redis, {"mode": "direct"})
            self.assertIsNone(redis.value)
            self.assertFalse(destination.exists())
            self.assertEqual(
                [path.name for path in Path(directory).iterdir()],
                [".config.json.lock"],
            )

    def test_failed_replace_does_not_rollback_newer_redis_write(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.json"
            redis = self.FakeRedis("old-redis")

            def concurrent_write_then_fail(*_args):
                redis.value = "newer-concurrent-value"
                raise OSError("synthetic failure")

            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", destination):
                with patch.object(proxy_pool.os, "replace", side_effect=concurrent_write_then_fail):
                    with redirect_stderr(io.StringIO()):
                        with self.assertRaisesRegex(RuntimeError, "代理配置未能完整保存"):
                            proxy_pool.save_proxy_config(redis, {"mode": "direct"})
            self.assertEqual(redis.value, "newer-concurrent-value")
            self.assertEqual(redis.eval_calls, 1)

    def test_staging_failure_leaves_redis_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            redis = self.FakeRedis("old-redis")
            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", Path(directory) / "config.json"):
                with patch.object(proxy_pool.tempfile, "NamedTemporaryFile", side_effect=OSError("synthetic failure")):
                    with redirect_stderr(io.StringIO()):
                        with self.assertRaisesRegex(RuntimeError, "代理配置未能完整保存"):
                            proxy_pool.save_proxy_config(redis, {"mode": "direct"})
            self.assertEqual(redis.value, "old-redis")
            self.assertEqual(redis.set_calls, 0)

    def test_successful_save_updates_both_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.json"
            redis = self.FakeRedis("old-redis")
            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", destination):
                saved = proxy_pool.save_proxy_config(redis, {"mode": "direct"})
            self.assertEqual(json.loads(redis.value), saved)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), saved)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((Path(directory) / ".config.json.lock").stat().st_mode), 0o600)
            self.assertEqual(redis.eval_calls, 0)

    @unittest.skipIf(os.name == "nt", "fcntl is POSIX only")
    def test_lock_excludes_a_second_open_file_descriptor(self):
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.json"
            lock_file = Path(directory) / ".config.json.lock"
            with proxy_pool._proxy_config_file_lock(config_file):
                second_descriptor = os.open(lock_file, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(second_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(second_descriptor)
            self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)

    def test_ambiguous_redis_set_failure_rolls_back_if_value_changed(self):
        secret = "synthetic-password-do-not-log"

        class ChangedThenFailedRedis(self.FakeRedis):
            def set(self, key, value):
                super().set(key, value)
                raise RuntimeError(secret)

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "config.json"
            redis = ChangedThenFailedRedis("old-redis")
            output = io.StringIO()
            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", destination):
                with redirect_stderr(output):
                    with self.assertRaisesRegex(RuntimeError, "代理配置未能完整保存"):
                        proxy_pool.save_proxy_config(redis, {"mode": "direct"})
            self.assertEqual(redis.value, "old-redis")
            self.assertFalse(destination.exists())
            self.assertNotIn(secret, output.getvalue())

    def test_status_only_contains_masked_credentials_and_known_fields(self):
        secret = "synthetic-secret-never-show"
        config = {
            "mode": "tunnel",
            "tunnel": f"http://demo:{secret}@gateway.invalid:8080",
            "api_url": f"https://gateway.invalid/list?token={secret}",
            "unexpected_secret_field": secret,
            "sticky_requests": 20,
        }
        displayed = proxy_pool.mask_proxy_config(config)
        self.assertEqual(displayed["tunnel_masked"], "http://demo:****@gateway.invalid:8080")
        self.assertEqual(displayed["tunnel"], displayed["tunnel_masked"])
        self.assertEqual(displayed["api_url"], "********")
        self.assertNotIn(secret, json.dumps(displayed))
        self.assertNotIn("unexpected_secret_field", displayed)

    def test_malformed_tunnel_never_echoes_password(self):
        secret = "malformed-secret"
        displayed = proxy_pool.mask_proxy_config({
            "tunnel": f"http://demo:{secret}@gateway.invalid:bad-port",
        })
        self.assertNotIn(secret, json.dumps(displayed))

    def test_proxy_url_query_credentials_are_hidden(self):
        secret = "query-secret"
        displayed = proxy_pool.mask_proxy_config({
            "tunnel": f"http://gateway.invalid:8080?token={secret}",
        })
        self.assertNotIn(secret, json.dumps(displayed))
        self.assertIn("[redacted]", displayed["tunnel_masked"])

    def test_local_config_replacement_is_owner_only_and_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            outside = folder / "unrelated.txt"
            outside.write_text("unrelated", encoding="utf-8")
            config_file = folder / "config.json"
            config_file.symlink_to(outside)
            with patch.object(proxy_pool, "LOCAL_PROXY_CONFIG_FILE", config_file):
                saved = proxy_pool.save_proxy_config(None, {
                    "mode": "tunnel",
                    "tunnel": "http://demo:synthetic-secret@gateway.invalid:8080",
                })
            self.assertEqual(outside.read_text(encoding="utf-8"), "unrelated")
            self.assertFalse(config_file.is_symlink())
            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            self.assertEqual(json.loads(config_file.read_text(encoding="utf-8"))["tunnel"], saved["tunnel"])
            self.assertEqual(
                sorted(path.name for path in folder.iterdir()),
                [".config.json.lock", "config.json", "unrelated.txt"],
            )


if __name__ == "__main__":
    unittest.main()
