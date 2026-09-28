"""Customer-release API guard tests; no service, queue, proxy, or network is used."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


API_PATH = Path(__file__).resolve().parents[1] / "tools" / "dashboard_api.py"


def load_customer_api():
    spec = importlib.util.spec_from_file_location("customer_release_dashboard_api", API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CustomerReleaseModeTests(unittest.TestCase):
    def setUp(self):
        self.launch_token = "customer_launch_token_123456"
        self.env_patch = patch.dict(os.environ, {
            "TPS_RELEASE_MODE": "customer",
            "TPS_RELEASE_LAUNCH_TOKEN": self.launch_token,
        }, clear=True)
        self.env_patch.start()
        self.api = load_customer_api()
        self.client = self.api.app.test_client()

    def tearDown(self):
        self.env_patch.stop()

    def test_customer_mode_blocks_write_and_external_action_endpoints_before_dependencies(self):
        for path, body, code in (
            ("/api/pipeline/worker", {"action": "start"}, "customer_release_control_surface_disabled"),
            ("/api/proxy/config", {"mode": "direct"}, "customer_release_control_surface_disabled"),
            ("/api/proxy/test", {"proxy": "direct"}, "customer_release_control_surface_disabled"),
            ("/api/cluster/control", {"action": "start"}, "customer_release_control_surface_disabled"),
            ("/api/system/apply-update", {}, "customer_release_read_only"),
            ("/api/batch100/start", {}, "customer_release_control_surface_disabled"),
        ):
            with self.subTest(path=path), patch.object(self.api, "get_redis") as get_redis:
                response = self.client.post(path, json=body)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json["code"], code)
            get_redis.assert_not_called()

    def test_customer_mode_never_checks_for_online_updates(self):
        response = self.client.get("/api/system/check-update")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json["code"], "customer_release_read_only")

    def test_customer_mode_rejects_direct_control_surface_reads_before_dependencies(self):
        for path in (
            "/api/pipeline",
            "/api/pipeline/scale",
            "/api/proxy/config",
            "/api/cluster/status",
            "/api/batch100/status",
        ):
            with self.subTest(path=path), patch.object(self.api, "get_redis") as get_redis:
                response = self.client.get(path)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json["code"], "customer_release_control_surface_disabled")
            get_redis.assert_not_called()

    def test_customer_shell_bootstraps_only_the_non_secret_route_mode(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('window.__TPS_RELEASE_MODE__="customer"', body)
        self.assertNotIn(self.launch_token, body)

    def test_customer_mode_exposes_local_version_but_labels_the_mode(self):
        response = self.client.get("/api/system/version")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertEqual(response.json["release_mode"], "customer")
        self.assertEqual(response.json["launch_token"], self.launch_token)

    def test_customer_launch_can_require_the_configured_port(self):
        args = self.api._build_parser().parse_args(
            ["--host", "127.0.0.1", "--port", "5123", "--strict-port"]
        )
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 5123)
        self.assertTrue(args.strict_port)

    def test_customer_version_refuses_a_missing_or_invalid_launch_token_without_echoing_it(self):
        for token in ("", "not a safe token"):
            with self.subTest(token=token), patch.dict(
                os.environ,
                {"TPS_RELEASE_LAUNCH_TOKEN": token},
                clear=False,
            ):
                response = self.client.get("/api/system/version")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json["code"], "customer_release_launch_token_unavailable")
            if token:
                self.assertNotIn(token, response.get_data(as_text=True))

    def test_standard_version_never_exposes_a_process_launch_token(self):
        with patch.dict(os.environ, {
            "TPS_RELEASE_MODE": "standard",
            "TPS_RELEASE_LAUNCH_TOKEN": self.launch_token,
        }, clear=True):
            standard_api = load_customer_api()
            response = standard_api.app.test_client().get("/api/system/version")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["release_mode"], "standard")
        self.assertNotIn("launch_token", response.json)


if __name__ == "__main__":
    unittest.main()
