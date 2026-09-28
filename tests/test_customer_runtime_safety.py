"""Read-only customer runtime guards; no collector, service, or network is used."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tps_alert
import tps_control


class _StaleControlRedis:
    """Only implements reads expected by the status path and records writes."""

    def __init__(self):
        self.deleted = []
        self._controls = {
            tps_control.CONTROL_WORKER_KEY: json.dumps({"started_at": 1, "args": {}}),
            tps_control.CONTROL_DISCOVER_KEY: json.dumps({"started_at": 2, "args": {}}),
            tps_control.HB_DISCOVER_KEY: json.dumps({"url": "local-status-only"}),
        }

    def get(self, key):
        return self._controls.get(key)

    def llen(self, key):
        return 0

    def scard(self, key):
        return 0

    def delete(self, key):
        self.deleted.append(key)


class CustomerRuntimeSafetyTests(unittest.TestCase):
    def _pipeline_status(self, redis, *, read_only: bool):
        metrics = MagicMock()
        metrics.snapshot.return_value = {"counters": {}, "latency": {}, "ts": 1}
        with patch.object(tps_control, "queue_stats", return_value={}), \
                patch.object(tps_control, "peek_processing", return_value=[]), \
                patch.object(tps_control, "peek_dlq", return_value=[]), \
                patch.object(tps_control, "cluster_status", return_value={}), \
                patch.object(tps_control, "find_role_pids", return_value=[]), \
                patch.object(tps_control, "_scan_heartbeats", return_value=[]), \
                patch("tps_metrics.get_metrics", return_value=metrics), \
                patch("tps_coverage.coverage_snapshot", return_value={}), \
                patch("tps_coverage.discover_summary", return_value={}), \
                patch("tps_coverage.migrate_seen_to_queued") as migrate:
            payload = tps_control.pipeline_status(redis, read_only=read_only)
        return payload, migrate

    def test_read_only_pipeline_status_never_deletes_stale_control_or_heartbeat_records(self):
        redis = _StaleControlRedis()
        payload, migrate = self._pipeline_status(redis, read_only=True)

        self.assertEqual(redis.deleted, [])
        migrate.assert_not_called()
        self.assertEqual(payload["worker"]["started_at"], 1)
        self.assertEqual(payload["discover"]["started_at"], 2)
        self.assertEqual(payload["discover"]["heartbeat"]["url"], "local-status-only")

    def test_standard_pipeline_status_keeps_legacy_cleanup_behavior(self):
        redis = _StaleControlRedis()
        _, migrate = self._pipeline_status(redis, read_only=False)

        migrate.assert_called_once_with(redis)
        self.assertEqual(
            redis.deleted,
            [
                tps_control.CONTROL_WORKER_KEY,
                tps_control.CONTROL_DISCOVER_KEY,
                tps_control.HB_DISCOVER_KEY,
            ],
        )

    def test_customer_mode_hard_disables_alert_even_with_explicit_url_and_force(self):
        output = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "TPS_RELEASE_MODE": "customer",
                "TPS_ALERT_WEBHOOK": "https://example.invalid/configured",
            },
            clear=True,
        ), patch.object(tps_alert.urllib.request, "urlopen") as open_url, redirect_stderr(output):
            sent = tps_alert.send_alert(
                "customer test",
                "must not leave this process",
                webhook_url="https://example.invalid/explicit",
                force=True,
            )

        self.assertFalse(sent)
        open_url.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_explicit_supervisor_outbound_guard_also_prevents_alert_delivery(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(tps_alert.urllib.request, "urlopen") as open_url:
            sent = tps_alert.send_alert(
                "supervisor guard",
                "must not leave this process",
                webhook_url="https://example.invalid/explicit",
                outbound_enabled=False,
            )

        self.assertFalse(sent)
        open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
