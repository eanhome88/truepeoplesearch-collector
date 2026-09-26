"""Local dashboard infrastructure checks; all service connections are mocked."""

import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


API_PATH = Path(__file__).resolve().parents[1] / "tools" / "dashboard_api.py"


def load_api():
    spec = importlib.util.spec_from_file_location("dashboard_api_local_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.dict(os.environ, {}, clear=True):
    api = load_api()


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_local_and_preserve_database_defaults(self):
        database, redis, dashboard = api._load_runtime_config({})
        self.assertEqual(database, {
            "host": "127.0.0.1", "port": 4000, "user": "root",
            "password": "", "database": "people_search", "autocommit": True,
        })
        self.assertEqual(redis, {"host": "127.0.0.1", "port": 6379})
        self.assertEqual(dashboard, {"host": "127.0.0.1", "port": 5001})

    def test_environment_overrides_are_loaded_without_connecting(self):
        settings = {
            "TPS_DB_HOST": "db.local", "TPS_DB_PORT": "4333", "TPS_DB_USER": "local",
            "TPS_DB_PASSWORD": "test-secret", "TPS_DB_NAME": "fixture",
            "TPS_REDIS_HOST": "cache.local", "TPS_REDIS_PORT": "6333",
            "TPS_DASHBOARD_HOST": "::1", "TPS_DASHBOARD_PORT": "5111",
        }
        with patch.dict(os.environ, settings, clear=True), \
                patch("mysql.connector.connect") as connect, patch("redis.Redis") as redis:
            configured = load_api()
        connect.assert_not_called()
        redis.assert_not_called()
        self.assertEqual(configured.TIDB_CONFIG["password"], "test-secret")
        self.assertEqual(configured.TIDB_CONFIG["host"], "db.local")
        self.assertEqual(configured.TIDB_CONFIG["port"], 4333)
        self.assertEqual(configured.TIDB_CONFIG["database"], "fixture")
        self.assertEqual(configured.TIDB_CONFIG["user"], "local")
        self.assertEqual(configured.REDIS_CONFIG, {"host": "cache.local", "port": 6333})
        self.assertEqual(vars(configured._build_parser().parse_args([])), {"host": "::1", "port": 5111})

    def test_invalid_ports_never_echo_values(self):
        for name in ("TPS_DB_PORT", "TPS_REDIS_PORT", "TPS_DASHBOARD_PORT"):
            for value in ("test-secret", "0", "65536", "1.5"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError) as error:
                    api._load_runtime_config({name: value, "TPS_DB_PASSWORD": "test-secret"})
                self.assertIn(name, str(error.exception))
                self.assertNotIn("test-secret", str(error.exception))

    def test_invalid_import_configuration_fails_without_connections_or_secret(self):
        with patch.dict(os.environ, {"TPS_DB_PORT": "test-secret", "TPS_DB_PASSWORD": "test-secret"}, clear=True), \
                patch("mysql.connector.connect") as connect, self.assertRaises(ValueError) as error:
            load_api()
        connect.assert_not_called()
        self.assertNotIn("test-secret", str(error.exception))

    def test_invalid_text_never_echoes_values(self):
        for name in ("TPS_DB_HOST", "TPS_DB_USER", "TPS_DB_NAME", "TPS_REDIS_HOST", "TPS_DASHBOARD_HOST"):
            for value in ("", "  ", "test-secret\n"):
                with self.subTest(name=name), self.assertRaises(ValueError) as error:
                    api._load_runtime_config({name: value})
                self.assertIn(name, str(error.exception))
                self.assertNotIn("test-secret", str(error.exception))

    def test_dashboard_bind_is_always_loopback(self):
        for host, expected in (("127.0.0.1", "127.0.0.1"), ("::1", "::1"), ("localhost", "127.0.0.1")):
            with self.subTest(host=host):
                self.assertEqual(api._load_runtime_config({"TPS_DASHBOARD_HOST": host})[2]["host"], expected)
                args = api._build_parser().parse_args(["--host", host, "--port", "5100"])
                self.assertEqual((args.host, args.port), (expected, 5100))

        for host in ("0.0.0.0", "::", "192.0.2.12", "127.0.0.2", "test-secret"):
            with self.subTest(rejected=host):
                with self.assertRaises(ValueError) as error:
                    api._load_runtime_config({"TPS_DASHBOARD_HOST": host})
                if host == "test-secret":
                    self.assertNotIn(host, str(error.exception))
                with patch("sys.stderr"):
                    with self.assertRaises(SystemExit):
                        api._build_parser().parse_args(["--host", host])

    def test_invalid_import_bind_fails_before_connections(self):
        with patch.dict(os.environ, {"TPS_DASHBOARD_HOST": "0.0.0.0"}, clear=True), \
                patch("mysql.connector.connect") as connect, self.assertRaises(ValueError):
            load_api()
        connect.assert_not_called()


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()
        driver_version = patch.object(api.mysql.connector, "__version__", "9.2.0")
        driver_version.start()
        self.addCleanup(driver_version.stop)

    def test_liveness_uses_no_services(self):
        with patch.object(api.mysql.connector, "connect") as connect, \
                patch("redis.Redis") as redis, patch.object(api, "get_db") as get_db, \
                patch.object(api, "get_redis") as get_redis:
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "status": "alive"})
        for service in (connect, redis, get_db, get_redis):
            service.assert_not_called()

    def test_readiness_checks_schema_without_reading_records_or_using_pools(self):
        connection, redis_client = MagicMock(), MagicMock()
        cursor = connection.cursor.return_value
        redis_client.ping.return_value = True
        with patch.object(api.mysql.connector, "connect", return_value=connection) as connect, \
                patch("redis.Redis", return_value=redis_client) as redis, \
                patch.object(api, "_get_pool") as pool, patch.object(api, "get_redis") as cached_redis:
            response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "status": "ready", "components": {
            "database": {"ok": True, "status": "ready"}, "redis": {"ok": True, "status": "ready"},
        }})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        cursor.execute.assert_called_once_with(
            "SELECT 1 FROM `persons` CROSS JOIN `aliases` CROSS JOIN `current_addresses` "
            "CROSS JOIN `previous_addresses` CROSS JOIN `phone_numbers` CROSS JOIN `email_addresses` "
            "CROSS JOIN `relatives` CROSS JOIN `associates` CROSS JOIN `stats_snapshot` LIMIT 0"
        )
        cursor.fetchall.assert_not_called()
        cursor.fetchone.assert_not_called()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()
        redis_client.close.assert_called_once()
        pool.assert_not_called()
        cached_redis.assert_not_called()
        self.assertEqual(connect.call_args.kwargs["connection_timeout"], 3)
        self.assertEqual(redis.call_args.kwargs["socket_connect_timeout"], 3)
        self.assertEqual(redis.call_args.kwargs["socket_timeout"], 3)
        self.assertEqual(redis.call_args.kwargs["retry"]._retries, 0)

    def test_dependency_failure_returns_503_without_exception_details(self):
        for failing in ("database", "redis", "both"):
            connection, redis_client = MagicMock(), MagicMock()
            redis_client.ping.return_value = True
            if failing in ("database", "both"):
                connection.cursor.return_value.execute.side_effect = RuntimeError("test-secret and server address")
            if failing in ("redis", "both"):
                redis_client.ping.side_effect = RuntimeError("test-secret and server address")
            with self.subTest(failing=failing), \
                    patch.object(api.mysql.connector, "connect", return_value=connection), \
                    patch("redis.Redis", return_value=redis_client):
                response = self.client.get("/api/ready")
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json["ok"])
            self.assertEqual(response.json["status"], "not_ready")
            self.assertEqual(response.json["components"]["database"]["ok"], failing == "redis")
            self.assertEqual(response.json["components"]["redis"]["ok"], failing == "database")
            self.assertNotIn("test-secret", response.get_data(as_text=True))
            self.assertNotIn("server address", response.get_data(as_text=True))
            connection.cursor.return_value.close.assert_called_once()
            connection.close.assert_called_once()
            redis_client.close.assert_called_once()

    def test_connection_failure_still_checks_other_component(self):
        redis_client = MagicMock()
        redis_client.ping.return_value = True
        with patch.object(api.mysql.connector, "connect", side_effect=RuntimeError("test-secret")), \
                patch("redis.Redis", return_value=redis_client):
            response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json["components"]["redis"]["ok"])
        self.assertNotIn("test-secret", response.get_data(as_text=True))

    def test_cleanup_failures_do_not_hide_probe_result(self):
        connection, redis_client = MagicMock(), MagicMock()
        redis_client.ping.return_value = True
        connection.cursor.return_value.close.side_effect = RuntimeError("test-secret")
        connection.close.side_effect = RuntimeError("test-secret")
        redis_client.close.side_effect = RuntimeError("test-secret")
        with patch.object(api.mysql.connector, "connect", return_value=connection), \
                patch("redis.Redis", return_value=redis_client):
            response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 200)
        connection.close.assert_called_once()
        redis_client.close.assert_called_once()

    def test_mysql_read_write_timeouts_are_required_for_readiness(self):
        for version in ("9.2.0", "26.7.0"):
            with self.subTest(version=version), patch.object(api.mysql.connector, "__version__", version):
                options = api._readiness_mysql_options()
            self.assertEqual(options["connection_timeout"], 3)
            self.assertEqual(options["read_timeout"], 3)
            self.assertEqual(options["write_timeout"], 3)
            self.assertNotIn("read_timeout", api.TIDB_CONFIG)

    def test_operational_pool_and_direct_connection_use_finite_timeouts(self):
        with patch.object(api.mysql.connector, "__version__", "26.7.0"), \
                patch.object(api, "_pool", None), \
                patch("mysql.connector.pooling.MySQLConnectionPool") as create_pool:
            api._get_pool()
        create_pool.assert_called_once()
        options = create_pool.call_args.kwargs
        self.assertEqual(options["connection_timeout"], api.DB_CONNECT_TIMEOUT_SEC)
        self.assertEqual(options["read_timeout"], api.DB_IO_TIMEOUT_SEC)
        self.assertEqual(options["write_timeout"], api.DB_IO_TIMEOUT_SEC)
        self.assertEqual(options["host"], api.TIDB_CONFIG["host"])
        self.assertNotIn("read_timeout", api.TIDB_CONFIG)

        with patch.object(api.mysql.connector, "__version__", "26.7.0"), \
                patch.object(api, "_pool", False), \
                patch.object(api.mysql.connector, "connect") as connect:
            with api.app.test_request_context():
                api.get_db()
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["connection_timeout"], api.DB_CONNECT_TIMEOUT_SEC)
        self.assertEqual(connect.call_args.kwargs["read_timeout"], api.DB_IO_TIMEOUT_SEC)
        self.assertEqual(connect.call_args.kwargs["write_timeout"], api.DB_IO_TIMEOUT_SEC)

    def test_old_driver_does_not_open_unbounded_operational_connection(self):
        with patch.object(api.mysql.connector, "__version__", "8.4.0"), \
                patch.object(api, "_pool", False), \
                patch.object(api.mysql.connector, "connect") as connect:
            with api.app.test_request_context(), self.assertRaises(api.ReadinessDriverUnsupported):
                api.get_db()
        connect.assert_not_called()

    def test_old_or_unknown_driver_fails_closed_without_database_connection(self):
        for version in ("8.4.0", "9.1.0", "unknown", "", None):
            redis_client = MagicMock()
            redis_client.ping.return_value = True
            with self.subTest(version=version), \
                    patch.object(api.mysql.connector, "__version__", version), \
                    patch.object(api.mysql.connector, "connect") as connect, \
                    patch("redis.Redis", return_value=redis_client):
                response = self.client.get("/api/ready")
                connect.assert_not_called()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json["components"]["database"], {
                "ok": False, "status": "unavailable", "reason": "mysql_connector_9_2_required",
            })
            self.assertTrue(response.json["components"]["redis"]["ok"])

    def test_shared_schema_probe_includes_all_required_tables_once(self):
        expected_tables = (
            "persons", "aliases", "current_addresses", "previous_addresses",
            "phone_numbers", "email_addresses", "relatives", "associates", "stats_snapshot",
        )
        self.assertEqual(api.REQUIRED_LOCAL_TABLES, expected_tables)
        for table in expected_tables:
            self.assertEqual(api.LOCAL_SCHEMA_CHECK_SQL.count(f"`{table}`"), 1)

    def test_shared_probe_preserves_failure_for_launcher_and_releases_connection(self):
        for errno in (1146, 1142):
            connection = MagicMock()
            error = api.mysql.connector.Error("test-secret", errno=errno)
            connection.cursor.return_value.execute.side_effect = error
            with self.subTest(errno=errno), \
                    patch.object(api.mysql.connector, "connect", return_value=connection), \
                    self.assertRaises(api.mysql.connector.Error) as caught:
                api.check_local_database_ready()
            self.assertEqual(caught.exception.errno, errno)
            connection.cursor.return_value.close.assert_called_once()
            connection.close.assert_called_once()


class TargetConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()
        self.control = SimpleNamespace(
            start_worker=MagicMock(return_value={"ok": True}),
            start_discover=MagicMock(return_value={"ok": True}),
            start_cluster=MagicMock(return_value={"ok": True}),
            stop_worker=MagicMock(return_value={"ok": True}),
            stop_discover=MagicMock(return_value={"ok": True}),
            stop_cluster=MagicMock(return_value={"ok": True}),
            cluster_status=MagicMock(return_value={}),
        )

    def _post(self, role, action):
        path = "/api/cluster/control" if role == "cluster" else f"/api/pipeline/{role}"
        return self.client.post(path, json={"action": action})

    def test_default_targets_are_read_from_worker_source_without_importing_it(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.dict(sys.modules, {"tps_control": self.control}), \
                patch.object(api, "get_redis", return_value=MagicMock()) as redis, \
                patch.object(api, "_pipeline_payload", return_value={}):
            self.assertEqual(api._background_database_target()["database"], "people_search")
            for role in ("worker", "discover", "cluster"):
                with self.subTest(role=role):
                    response = self._post(role, "start")
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json["ok"])
        self.assertEqual(redis.call_count, 3)
        for name in ("start_worker", "start_discover", "start_cluster"):
            getattr(self.control, name).assert_called_once()

    def test_mismatched_dashboard_redis_blocks_every_start_before_redis_access(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(api, "REDIS_CONFIG", {"host": "other.local", "port": 6379}), \
                patch.object(api, "get_redis") as redis, \
                patch.dict(sys.modules, {"tps_control": self.control}):
            for role in ("worker", "discover", "cluster"):
                with self.subTest(role=role):
                    response = self._post(role, "start")
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json["code"], "background_target_mismatch")
        redis.assert_not_called()
        for name in ("start_worker", "start_discover", "start_cluster"):
            getattr(self.control, name).assert_not_called()

    def test_mismatched_dashboard_database_blocks_worker_and_cluster_only(self):
        mismatch = dict(api.TIDB_CONFIG, database="another_database")
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(api, "TIDB_CONFIG", mismatch), \
                patch.object(api, "get_redis", return_value=MagicMock()) as redis, \
                patch.object(api, "_pipeline_payload", return_value={}), \
                patch.dict(sys.modules, {"tps_control": self.control}):
            for role in ("worker", "cluster"):
                with self.subTest(role=role):
                    response = self._post(role, "start")
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json["code"], "background_target_mismatch")
            self.assertEqual(self._post("discover", "start").status_code, 200)
        redis.assert_called_once()
        self.control.start_worker.assert_not_called()
        self.control.start_cluster.assert_not_called()
        self.control.start_discover.assert_called_once()

    def test_background_environment_override_is_checked_at_request_time(self):
        with patch.dict(os.environ, {"REDIS_HOST": "other.local"}, clear=True), \
                patch.object(api, "get_redis") as redis:
            response = self._post("discover", "start")
        self.assertEqual(response.status_code, 409)
        redis.assert_not_called()

    def test_unverifiable_database_target_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unknown.py"
            source.write_text("TIDB_CONFIG = {}\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(api, "_BACKGROUND_DB_SOURCE", source), \
                    patch.object(api, "get_redis") as redis:
                for role in ("worker", "cluster"):
                    with self.subTest(role=role):
                        response = self._post(role, "start")
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json["code"], "background_target_unverifiable")
            redis.assert_not_called()

    def test_stop_remains_available_when_targets_differ(self):
        with patch.dict(os.environ, {"REDIS_HOST": "other.local"}, clear=True), \
                patch.object(api, "REDIS_CONFIG", {"host": "different.local", "port": 6380}), \
                patch.object(api, "TIDB_CONFIG", dict(api.TIDB_CONFIG, database="another_database")), \
                patch.object(api, "get_redis", return_value=MagicMock()) as redis, \
                patch.object(api, "_pipeline_payload", return_value={}), \
                patch.dict(sys.modules, {"tps_control": self.control}):
            for role in ("worker", "discover", "cluster"):
                with self.subTest(role=role):
                    self.assertEqual(self._post(role, "stop").status_code, 200)
        self.assertEqual(redis.call_count, 3)
        for name in ("stop_worker", "stop_discover", "stop_cluster"):
            getattr(self.control, name).assert_called_once()

    def test_read_only_status_remains_available_when_targets_differ(self):
        proxy = SimpleNamespace(
            load_proxy_config=MagicMock(return_value={}),
            mask_proxy_config=MagicMock(return_value={}),
        )
        with patch.dict(os.environ, {"REDIS_HOST": "other.local"}, clear=True), \
                patch.object(api, "REDIS_CONFIG", {"host": "different.local", "port": 6380}), \
                patch.object(api, "get_redis", return_value=MagicMock()) as redis, \
                patch.object(api, "_pipeline_payload", return_value={}), \
                patch.dict(sys.modules, {"tps_control": self.control, "proxy_pool": proxy}):
            self.assertEqual(self.client.get("/api/pipeline").status_code, 200)
            self.assertEqual(self.client.get("/api/cluster/status").status_code, 200)
        self.assertEqual(redis.call_count, 1)

    def test_refused_control_action_is_not_reported_as_success(self):
        refused = {"ok": False, "error": "进程归属无法确认", "still_running": [42]}
        for name in (
            "start_worker", "start_discover", "start_cluster",
            "stop_worker", "stop_discover", "stop_cluster",
        ):
            getattr(self.control, name).return_value = refused
        self.control.cluster_status.return_value = {"running": True}
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(api, "get_redis", return_value=MagicMock()), \
                patch.object(api, "_pipeline_payload", return_value={"worker": {"running": True}}), \
                patch.dict(sys.modules, {"tps_control": self.control}):
            for action in ("start", "stop"):
                for role in ("worker", "discover", "cluster"):
                    with self.subTest(action=action, role=role):
                        response = self._post(role, action)
                        self.assertEqual(response.status_code, 409)
                        self.assertFalse(response.json["ok"])
                        self.assertEqual(response.json["error"], refused["error"])
                        self.assertEqual(response.json["result"], refused)
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                        if role == "cluster":
                            self.assertTrue(response.json["running"])
                        else:
                            self.assertEqual(response.json["worker"], {"running": True})


class BrowserBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()

    def test_only_local_host_headers_are_accepted(self):
        for host in ("localhost", "localhost:5001", "127.0.0.1:5001", "[::1]:5001"):
            with self.subTest(host=host):
                response = self.client.get("/api/health", headers={"Host": host})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        for host in ("evil.example", "localhost.evil.example", "0.0.0.0:5001", "127.0.0.2", "localhost:invalid"):
            with self.subTest(rejected=host):
                response = self.client.get("/api/health", headers={"Host": host})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_write_origin_must_match_local_host_and_scheme(self):
        path = "/api/pipeline/worker"
        for host, origin in (
            ("localhost:5001", "http://localhost:5001"),
            ("127.0.0.1:5001", "http://127.0.0.1:5001"),
            ("[::1]:5001", "http://[::1]:5001"),
        ):
            with self.subTest(host=host, origin=origin), patch.object(api, "get_redis") as redis:
                response = self.client.post(path, json={"action": "invalid"}, headers={"Host": host, "Origin": origin})
                self.assertEqual(response.status_code, 400)
                redis.assert_not_called()
        for origin in ("http://evil.example", "http://127.0.0.1:5001", "https://localhost:5001", "null"):
            with self.subTest(rejected=origin), patch.object(api, "get_redis") as redis:
                response = self.client.post(path, json={"action": "invalid"}, headers={"Host": "localhost:5001", "Origin": origin})
                self.assertEqual(response.status_code, 403)
                redis.assert_not_called()
        response = self.client.post(path, json={"action": "invalid"}, headers={"Host": "localhost:5001"})
        self.assertEqual(response.status_code, 400)

    def test_person_and_search_responses_are_never_cached(self):
        with patch.object(api, "query_one", return_value=None), patch.object(api, "query", return_value=[]):
            detail = self.client.get("/api/person/fixture")
            search = self.client.get("/api/search?q=fixture")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(search.status_code, 200)
        self.assertEqual(detail.headers["Cache-Control"], "no-store")
        self.assertEqual(search.headers["Cache-Control"], "no-store")


class ErrorResponseTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()

    def _proxy_transport(self, response):
        proxy = SimpleNamespace(load_proxy_config=MagicMock())
        fetcher = ModuleType("protocol_fetcher")
        fetcher.DEFAULT_HEADERS = {}
        fetcher.check_cloudflare_blocked = lambda _status, _html: False
        curl = ModuleType("curl_cffi")
        curl.__path__ = []
        requests_module = ModuleType("curl_cffi.requests")
        session = MagicMock()
        session.__enter__.return_value.get.return_value = response
        requests_module.Session = MagicMock(return_value=session)
        return {
            "proxy_pool": proxy, "protocol_fetcher": fetcher,
            "curl_cffi": curl, "curl_cffi.requests": requests_module,
        }, session

    def test_database_exception_and_unhandled_exception_never_reach_client(self):
        secret = "test-secret and private database address"
        with patch.object(api, "query_one", side_effect=RuntimeError(secret)):
            detail = self.client.get("/api/person/example")
        self.assertEqual(detail.status_code, 500)
        self.assertEqual(detail.json, {"error": api.PUBLIC_INTERNAL_ERROR})
        self.assertNotIn(secret, detail.get_data(as_text=True))

        with patch.object(api, "query", return_value=[]), \
                patch.object(api, "query_one", side_effect=RuntimeError(secret)):
            listing = self.client.get("/api/persons")
        self.assertEqual(listing.status_code, 500)
        self.assertEqual(listing.json, {"error": api.PUBLIC_INTERNAL_ERROR})
        self.assertNotIn(secret, listing.get_data(as_text=True))

    def test_pipeline_exception_is_fixed_and_preserves_status_shape(self):
        with patch.object(api, "_pipeline_payload", side_effect=RuntimeError("test-secret")):
            response = self.client.get("/api/pipeline")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json, {"error": api.PUBLIC_INTERNAL_ERROR, "redis_ok": False})

    def test_proxy_config_masked_api_url_preserves_existing_value(self):
        existing = {"tunnel": "", "api_url": "https://user:test-secret@example.invalid/path"}
        proxy = SimpleNamespace(
            load_proxy_config=MagicMock(return_value=existing),
            save_proxy_config=MagicMock(side_effect=lambda _redis, cfg: cfg),
            mask_proxy_config=MagicMock(return_value={"api_url": "********"}),
        )
        with patch.dict(sys.modules, {"proxy_pool": proxy}), \
                patch.object(api, "get_redis", return_value=MagicMock()):
            response = self.client.post("/api/proxy/config", json={"mode": "api", "api_url": "********"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["config"]["api_url"], "********")
        self.assertEqual(proxy.save_proxy_config.call_args.args[1]["api_url"], existing["api_url"])
        self.assertNotIn("test-secret", response.get_data(as_text=True))

    def test_proxy_config_save_failure_is_not_reported_as_success(self):
        proxy = SimpleNamespace(
            load_proxy_config=MagicMock(return_value={}),
            save_proxy_config=MagicMock(side_effect=RuntimeError("test-secret and private path")),
            mask_proxy_config=MagicMock(),
        )
        with patch.dict(sys.modules, {"proxy_pool": proxy}), \
                patch.object(api, "get_redis", return_value=MagicMock()):
            response = self.client.post("/api/proxy/config", json={"mode": "direct"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json, {"ok": False, "error": api.PUBLIC_INTERNAL_ERROR})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("test-secret", response.get_data(as_text=True))
        proxy.mask_proxy_config.assert_not_called()

    def test_proxy_config_get_can_use_local_fallback_but_post_requires_redis(self):
        proxy = SimpleNamespace(
            load_proxy_config=MagicMock(return_value={"mode": "direct"}),
            save_proxy_config=MagicMock(),
            mask_proxy_config=MagicMock(return_value={"mode": "direct"}),
        )
        with patch.dict(sys.modules, {"proxy_pool": proxy}), \
                patch.object(api, "get_redis", return_value=None):
            response = self.client.post("/api/proxy/config", json={"mode": "direct"})
            fallback = self.client.get("/api/proxy/config")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ok"])
        self.assertNotIn("已保存", response.get_data(as_text=True))
        proxy.save_proxy_config.assert_not_called()
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.json["config"], {"mode": "direct"})

    def test_proxy_probe_rejects_arbitrary_and_private_targets_before_transport(self):
        targets = (
            "http://localhost:6379/", "http://127.0.0.1:4000/",
            "http://10.0.0.5/", "http://192.168.1.1/",
            "https://www.truepeoplesearch.com/other",
            api.PROXY_TEST_URL + "?next=http://127.0.0.1/",
            "https://www.truepeoplesearch.com.evil.example/find/person/px82l44nur68u2l2l8n60",
        )
        with patch.object(api, "get_redis") as redis:
            for target in targets:
                with self.subTest(target=target):
                    response = self.client.post("/api/proxy/test", json={"proxy": "direct", "url": target})
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json["ok"])
        redis.assert_not_called()

    def test_proxy_probe_timeout_is_bounded_before_transport(self):
        with patch.object(api, "get_redis") as redis:
            for timeout in (0, 2, 21, 100000, "invalid"):
                with self.subTest(timeout=timeout):
                    response = self.client.post("/api/proxy/test", json={"proxy": "direct", "timeout": timeout})
                    self.assertEqual(response.status_code, 400)
        redis.assert_not_called()

    def test_proxy_probe_streams_fixed_target_without_following_redirects(self):
        for status in (200, 302):
            with self.subTest(status=status):
                response_body = MagicMock(status_code=status)
                response_body.iter_content.return_value = iter((b"<html>", b"fixture</html>"))
                modules, session = self._proxy_transport(response_body)
                with patch.dict(sys.modules, modules), \
                        patch.object(api, "get_redis", return_value=None):
                    result = self.client.post("/api/proxy/test", json={"proxy": "direct", "timeout": 15})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json["success"], status == 200)
                self.assertEqual(result.json["bytes"], len(b"<html>fixture</html>"))
                self.assertEqual(session.__enter__.return_value.get.call_args.args[0], api.PROXY_TEST_URL)
                self.assertEqual(session.__enter__.return_value.get.call_args.kwargs["timeout"], 15)
                self.assertFalse(session.__enter__.return_value.get.call_args.kwargs["allow_redirects"])
                self.assertTrue(session.__enter__.return_value.get.call_args.kwargs["stream"])
                response_body.iter_content.assert_called_once_with(chunk_size=8192)
                response_body.close.assert_called_once()

    def test_proxy_probe_stops_reading_at_response_limit(self):
        response_body = MagicMock(status_code=200)
        def chunks():
            for _ in range(33):
                yield b"x" * 8192
            raise AssertionError("stream was read beyond the size limit")
        response_body.iter_content.return_value = chunks()
        modules, session = self._proxy_transport(response_body)
        with patch.dict(sys.modules, modules), patch.object(api, "get_redis", return_value=None):
            result = self.client.post("/api/proxy/test", json={"proxy": "direct"})
        self.assertEqual(result.status_code, 413)
        self.assertFalse(result.json["success"])
        response_body.close.assert_called_once()
        session.__enter__.return_value.get.assert_called_once()

    def test_proxy_probe_exception_does_not_return_connection_details(self):
        proxy = SimpleNamespace(load_proxy_config=MagicMock())
        fetcher = ModuleType("protocol_fetcher")
        fetcher.DEFAULT_HEADERS = {}
        fetcher.check_cloudflare_blocked = lambda _status, _html: False
        curl = ModuleType("curl_cffi")
        curl.__path__ = []
        requests_module = ModuleType("curl_cffi.requests")
        requests_module.Session = MagicMock(side_effect=RuntimeError("test-secret and private proxy address"))
        with patch.dict(sys.modules, {
            "proxy_pool": proxy, "protocol_fetcher": fetcher,
            "curl_cffi": curl, "curl_cffi.requests": requests_module,
        }), patch.object(api, "get_redis", return_value=None):
            response = self.client.post("/api/proxy/test", json={"proxy": "direct"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["error"], api.PUBLIC_INTERNAL_ERROR)
        self.assertNotIn("test-secret", response.get_data(as_text=True))
        requests_module.Session.assert_called_once()


class AssetTests(unittest.TestCase):
    def test_fixed_asset_supports_conditional_cache_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(api, "__file__", str(Path(directory) / "dashboard_api.py")):
                client = api.app.test_client()
                for name, mimetype, content in (
                    ("dashboard-runtime.js", "text/javascript", "window.runtimeFixture = true;\n"),
                    ("dashboard-app.js", "text/javascript", "window.appFixture = true;\n"),
                    ("dashboard.css", "text/css", "body { color: black; }\n"),
                ):
                    with self.subTest(asset=name):
                        asset = Path(directory) / name
                        asset.write_text(content, encoding="utf-8")
                        url = "/assets/" + name
                        response = client.get(url + "?path=dashboard_api.py")
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.get_data(as_text=True), content)
                        self.assertEqual(response.headers["Cache-Control"], "no-cache")
                        self.assertEqual(response.mimetype, mimetype)
                        etag = response.headers["ETag"]
                        response.close()
                        cached = client.get(url, headers={"If-None-Match": etag})
                        self.assertEqual(cached.status_code, 304)
                        self.assertEqual(cached.data, b"")
                        cached.close()
                        head = client.head(url)
                        self.assertEqual(head.status_code, 200)
                        self.assertEqual(head.data, b"")
                        self.assertEqual(head.headers["ETag"], etag)
                        head.close()

                        changed_content = content + "/* fixture updated */\n"
                        asset.write_text(changed_content, encoding="utf-8")
                        updated = client.get(url, headers={"If-None-Match": etag})
                        self.assertEqual(updated.status_code, 200)
                        self.assertEqual(updated.get_data(as_text=True), changed_content)
                        self.assertNotEqual(updated.headers["ETag"], etag)
                        updated.close()

    def test_shell_and_all_assets_load_without_service_connections(self):
        with patch.object(api, "get_db") as database, \
                patch.object(api, "get_redis") as redis, \
                patch.object(api, "_get_pool") as pool:
            client = api.app.test_client()
            with client.get("/") as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                html = response.get_data(as_text=True)
            for name in ("dashboard-runtime.js", "dashboard-app.js", "dashboard.css"):
                self.assertIn("/assets/" + name, html)
                with client.get("/assets/" + name) as asset:
                    self.assertEqual(asset.status_code, 200)
                    self.assertGreater(len(asset.data), 0)
        for service in (database, redis, pool):
            service.assert_not_called()

    def test_assets_do_not_expose_directories_or_arbitrary_files(self):
        client = api.app.test_client()
        for path in ("/assets/", "/assets/dashboard_api.py", "/assets/dashboard.html", "/assets/../dashboard_api.py", "/assets/%2e%2e/dashboard_api.py", "/assets/dashboard.css/../dashboard_api.py"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)


class PortTests(unittest.TestCase):
    def test_probe_uses_exact_loopback_bind_host(self):
        for host, bind_host, family in (("127.0.0.1", "127.0.0.1", socket.AF_INET), ("localhost", "127.0.0.1", socket.AF_INET), ("::1", "::1", socket.AF_INET6)):
            with self.subTest(host=host), patch("socket.socket") as socket_factory:
                self.assertEqual(api.find_available_port(host, 5001), 5001)
            socket_factory.assert_called_once_with(family, socket.SOCK_STREAM)
            socket_factory.return_value.__enter__.return_value.bind.assert_called_once_with((bind_host, 5001))

    def test_probe_rejects_nonlocal_host_before_socket_creation(self):
        for host in ("0.0.0.0", "::", "192.0.2.12"):
            with self.subTest(host=host), patch("socket.socket") as socket_factory:
                with self.assertRaises(ValueError):
                    api.find_available_port(host, 5001)
                socket_factory.assert_not_called()

    def test_probe_can_skip_busy_port(self):
        with patch("socket.socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.bind.side_effect = [OSError("occupied"), None]
            self.assertEqual(api.find_available_port("127.0.0.1", 5001), 5002)
        self.assertEqual(probe.bind.call_count, 2)

    def test_all_busy_ports_raise_instead_of_returning_unusable_port(self):
        with patch("socket.socket") as socket_factory:
            probe = socket_factory.return_value.__enter__.return_value
            probe.bind.side_effect = OSError("occupied")
            with self.assertRaisesRegex(RuntimeError, "No available dashboard port"):
                api.find_available_port("127.0.0.1", 65535)
            self.assertEqual(probe.bind.call_count, 1)

    def test_invalid_port_or_attempts_fail_before_socket_creation(self):
        for port, attempts in ((0, 20), (65536, 20), (5001, 0)):
            with self.subTest(port=port, attempts=attempts), patch("socket.socket") as socket_factory:
                with self.assertRaises(ValueError):
                    api.find_available_port("127.0.0.1", port, attempts)
                socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
