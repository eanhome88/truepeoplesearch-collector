"""Aggregate monitoring must distinguish measured zero, unknown, and invalid.

Only in-memory Redis and mocked DB aggregates are used. No collector is started.
"""

from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import fakeredis


API_PATH = Path(__file__).resolve().parents[1] / "tools" / "dashboard_api.py"
with patch.dict(os.environ, {}, clear=True):
    spec = importlib.util.spec_from_file_location("dashboard_monitoring_truth", API_PATH)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

from tps_metrics import BUCKETS, COUNTER_KEY, get_metrics


class MonitoringTruthTests(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.metrics = get_metrics(self.redis)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(api, "get_redis", return_value=self.redis))
        self.db = self.stack.enter_context(patch.object(api, "query_one", return_value={
            "persons": 87, "phones": 90, "emails": 40, "prev_addr": 1, "aliases": 0,
            "wireless_count": 50, "primary_wireless_count": 40, "smart_fallback_count": 10,
        }))
        self.workers = self.stack.enter_context(patch("tps_control._scan_heartbeats", return_value=[]))
        self.stack.enter_context(patch("tps_control.find_role_pids", return_value=[]))
        self.stack.enter_context(patch("tps_coverage.coverage_snapshot", return_value={}))
        self.stack.enter_context(patch("tps_coverage.scale_snapshot", return_value={}))
        self.queue = self.stack.enter_context(patch("tps_queue.queue_stats", return_value={"total": 50000}))

    def put_counters(self, **values):
        for key, value in values.items():
            self.redis.set(COUNTER_KEY.format(bucket=key), value)

    def put_demo_fixture(self):
        self.put_counters(attempt=8560, success=8542, dedup_hit=1820)
        self.redis.set("tps:metrics:lat:count:scrape_ms", 8542)
        self.redis.set("tps:metrics:lat:sum:scrape_ms", 367306.0)

    def test_database_stock_and_queue_do_not_create_runtime_success(self):
        stats = api._load_stats()
        self.assertEqual(stats["persons"], 87)
        self.assertEqual(stats["total_tasks_executed"], 0)
        self.assertEqual(stats["success_tasks"], 0)
        self.assertIsNone(stats["success_rate_pct"])
        self.assertIsNone(stats["avg_latency_ms"])
        self.assertEqual(stats["current_qps"], 0.0)
        self.assertTrue(stats["throughput_available"])
        self.assertEqual(stats["metrics_quality"]["status"], "no_samples")

    def test_valid_hundred_percent_is_not_artificially_capped(self):
        self.metrics.incr("success", 5)
        stats = api._load_stats()
        self.assertEqual(stats["total_tasks_executed"], 5)
        self.assertEqual(stats["success_rate_pct"], 100.0)
        self.assertEqual(stats["completion_rate_pct"], 100.0)

    def test_measured_zero_success_and_latency_are_preserved(self):
        self.metrics.incr("rate_limit", 2)
        self.metrics.observe_ms("scrape_ms", 0.0)
        stats = api._load_stats()
        self.assertEqual(stats["success_tasks"], 0)
        self.assertEqual(stats["success_rate_pct"], 0.0)
        self.assertEqual(stats["avg_latency_ms"], 0.0)

    def test_success_and_completion_use_the_same_attempt_denominator(self):
        self.metrics.incr("success", 2)
        self.metrics.incr("empty", 3)
        snap = self.metrics.snapshot()
        self.assertEqual(snap["success_rate_pct"], 40.0)
        self.assertEqual(snap["completion_rate_pct"], 100.0)

    def test_impossible_completion_is_unknown_not_clamped_to_success(self):
        self.put_counters(attempt=3, success=2, empty=3)
        snap = self.metrics.snapshot()
        self.assertEqual(snap["counters"]["success"], 2)
        self.assertIsNone(snap["success_rate_pct"])
        self.assertIsNone(snap["completion_rate_pct"])
        self.assertIn("outcomes_exceed_attempts", snap["metrics_quality"]["issues"])

    def test_failure_outcomes_are_included_in_consistency_check(self):
        self.put_counters(attempt=5, success=4, rate_limit=2)
        self.assertEqual(self.metrics.snapshot()["metrics_quality"]["status"], "inconsistent")

    def test_negative_counter_is_not_a_valid_rate(self):
        self.put_counters(attempt=10, success=-1)
        snap = self.metrics.snapshot()
        self.assertIsNone(snap["success_rate_pct"])
        self.assertIn("negative_counter", snap["metrics_quality"]["issues"])

    def test_demo_compatible_values_are_flagged_without_mutation(self):
        self.put_demo_fixture()
        keys_before = {key: self.redis.get(key) for key in self.redis.scan_iter()}
        stats = api._load_stats()
        self.assertIn("demo_compatible_counters", stats["metrics_quality"]["issues"])
        self.assertIsNone(stats["success_rate_pct"])
        self.assertIsNone(stats["avg_latency_ms"])
        self.assertEqual(keys_before, {key: self.redis.get(key) for key in self.redis.scan_iter()})

    def test_all_traffic_savings_are_unknown_without_byte_measurements(self):
        self.metrics.incr("success", 100)
        self.metrics.incr("dedup_hit", 50)
        stats = api._load_stats()
        for key in ("traffic_saved_mb", "traffic_saved_gb", "traffic_saved_ratio_pct"):
            self.assertIsNone(stats[key])
        self.assertFalse(stats["traffic_measurement_available"])

    def test_observed_worker_throughput_is_used(self):
        self.workers.return_value = [{"current_qps": 1.5}, {"current_qps": 2.0}]
        self.assertEqual(api._load_stats()["current_qps"], 3.5)

    def test_worker_without_measurement_is_not_reported_as_idle(self):
        self.workers.return_value = [{"status": "running"}]
        stats = api._load_stats()
        self.assertIsNone(stats["current_qps"])
        self.assertFalse(stats["throughput_available"])

    def test_invalid_worker_rates_are_unknown(self):
        for value in (None, "invalid", float("nan"), float("inf"), -1):
            with self.subTest(value=value):
                self.workers.return_value = [{"current_qps": value}]
                self.assertIsNone(api._load_stats()["current_qps"])

    def test_overflowing_worker_rate_sum_is_unknown_and_json_safe(self):
        self.workers.return_value = [{"current_qps": 1e308}, {"current_qps": 1e308}]
        stats = api._load_stats()
        self.assertIsNone(stats["current_qps"])
        self.assertFalse(stats["throughput_available"])
        json.dumps(stats, allow_nan=False)

    def test_heartbeat_read_error_is_not_reported_as_idle(self):
        self.workers.side_effect = RuntimeError("synthetic unavailable")
        self.assertIsNone(api._load_stats()["current_qps"])

    def test_unavailable_redis_is_explicit_not_zero(self):
        with patch.object(api, "get_redis", return_value=None):
            stats = api._load_stats()
        self.assertFalse(stats["metrics_available"])
        self.assertEqual(stats["metrics_quality"]["status"], "unavailable")
        self.assertIsNone(stats["total_tasks_executed"])
        self.assertIsNone(stats["success_tasks"])
        self.assertIsNone(stats["current_qps"])
        self.assertEqual(stats["persons"], 87)

    def test_counter_read_error_is_explicit_not_zero(self):
        with patch("tps_metrics.Metrics.snapshot", side_effect=RuntimeError("synthetic unavailable")):
            stats = api._load_stats()
        self.assertFalse(stats["metrics_available"])
        self.assertIsNone(stats["success_rate_pct"])

    def test_database_failure_is_unknown_not_an_empty_database(self):
        self.db.side_effect = RuntimeError("synthetic unavailable")
        stats = api._load_stats()
        self.assertFalse(stats["database_available"])
        self.assertIsNone(stats["persons"])
        self.assertIsNone(stats["wireless_ratio_pct"])

    def test_legacy_schema_does_not_invent_primary_selection_stats(self):
        self.db.side_effect = [RuntimeError("synthetic legacy schema"), {
            "persons": 1, "phones": 1, "emails": 0, "prev_addr": 0, "aliases": 0, "wireless_count": 1,
        }]
        stats = api._load_stats()
        self.assertTrue(stats["database_available"])
        self.assertIsNone(stats["primary_wireless_count"])
        self.assertIsNone(stats["smart_fallback_count"])

    def test_metrics_endpoint_marks_dependency_unavailable(self):
        with patch.object(api, "get_redis", return_value=None):
            response = api.app.test_client().get("/api/metrics")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["metrics_available"])
        self.assertTrue(all(data["counters"][key] is None for key in BUCKETS))

    def test_metrics_endpoint_reports_inconsistent_fixture(self):
        self.put_demo_fixture()
        self.put_counters(empty=4465)
        response = api.app.test_client().get("/api/metrics")
        data = response.get_json()
        self.assertIsNone(data["completion_rate_pct"])
        self.assertIn("demo_compatible_counters", data["metrics_quality"]["issues"])

    def test_missing_and_malformed_latency_samples_do_not_create_latency(self):
        self.assertIsNone(self.metrics.snapshot()["latency"]["scrape_ms"]["avg"])
        self.redis.set("tps:metrics:lat:sum:scrape_ms", 100)
        snap = self.metrics.snapshot()
        self.assertIsNone(snap["latency"]["scrape_ms"]["avg"])
        self.assertIn("invalid_latency_samples", snap["metrics_quality"]["issues"])

    def test_missing_latency_sum_is_not_measured_zero(self):
        self.redis.set("tps:metrics:lat:count:scrape_ms", 2)
        snap = self.metrics.snapshot()
        self.assertIsNone(snap["latency"]["scrape_ms"]["avg"])
        self.assertIn("invalid_latency_samples", snap["metrics_quality"]["issues"])

    def test_nonfinite_latency_does_not_break_json_or_prometheus(self):
        self.redis.set("tps:metrics:lat:count:scrape_ms", 2)
        self.redis.set("tps:metrics:lat:sum:scrape_ms", "nan")
        snap = self.metrics.snapshot()
        self.assertIsNone(snap["latency"]["scrape_ms"]["avg"])
        self.assertIsNone(snap["latency"]["scrape_ms"]["sum"])
        json.dumps(snap, allow_nan=False)
        self.assertIn('tps_latency_ms_sum{metric="scrape_ms"} NaN', self.metrics.render_prometheus())


if __name__ == "__main__":
    unittest.main()
