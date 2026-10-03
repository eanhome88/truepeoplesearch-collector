"""共享入库速率只计一次；丢失监测时显示未知而非假 0。"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import fakeredis

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))

import bulk_ingester_daemon
import dashboard_api
import tps_control


class SharedIngestRateTests(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)

    def publish(self, qps=7.5, age=0):
        self.redis.set(tps_control.BULK_COMMITTED_TOTAL_KEY, 42)
        self.redis.set(tps_control.BULK_RATE_KEY, json.dumps({
            "qps": qps, "committed_total": 42, "updated_at": time.time() - age,
            "db_ready": True, "db_target": "synthetic-target",
        }))

    def beats(self):
        return [
            {"worker_id": "a", "current_qps": 0.0, "decoupled_ingest": True, "concurrency": 2},
            {"worker_id": "b", "current_qps": 0.0, "decoupled_ingest": True, "concurrency": 2},
        ]

    def test_two_decoupled_workers_do_not_double_count_one_bulk_rate(self):
        self.publish()
        self.redis.lpush("tps:buffer:parsed", "pending")
        self.redis.lpush("tps:buffer:processing", "claimed")
        with mock.patch.object(tps_control, "find_cluster_pids", return_value=[]), \
             mock.patch.object(tps_control, "_scan_heartbeats", return_value=self.beats()), \
             mock.patch.object(tps_control, "_read_control", return_value={}), \
             mock.patch.object(tps_control, "_tail_log", return_value=[]):
            cluster = tps_control.cluster_status(self.redis)
            self.assertTrue(cluster["throughput_available"])
            self.assertEqual(cluster["total_qps"], 7.5)
            self.assertEqual(cluster["buffer_depth"], 2)
            self.assertEqual(cluster["bulk_committed_total"], 42)

            with mock.patch.object(dashboard_api, "get_redis", return_value=self.redis), \
                 mock.patch.object(dashboard_api, "query_one", return_value=None):
                stats = dashboard_api._load_stats()
        self.assertEqual(stats["current_qps"], 7.5)
        self.assertEqual(stats["bulk_ingest_qps"], 7.5)
        self.assertEqual(stats["current_qps_meaning"], "database_persisted_confirmed_per_second")

    def test_missing_or_stale_bulk_rate_is_unknown_while_decoupled_workers_run(self):
        for age in (None, 60):
            with self.subTest(age=age):
                self.redis.delete(tps_control.BULK_RATE_KEY)
                if age is not None:
                    self.publish(age=age)
                with mock.patch.object(tps_control, "find_cluster_pids", return_value=[]), \
                     mock.patch.object(tps_control, "_scan_heartbeats", return_value=self.beats()), \
                     mock.patch.object(tps_control, "_read_control", return_value={}), \
                     mock.patch.object(tps_control, "_tail_log", return_value=[]):
                    cluster = tps_control.cluster_status(self.redis)
                    self.assertFalse(cluster["throughput_available"])
                    self.assertIsNone(cluster["total_qps"])
                    with mock.patch.object(dashboard_api, "get_redis", return_value=self.redis), \
                         mock.patch.object(dashboard_api, "query_one", return_value=None):
                        stats = dashboard_api._load_stats()
                self.assertFalse(stats["throughput_available"])
                self.assertIsNone(stats["current_qps"])

    def test_database_not_ready_does_not_publish_a_false_zero_rate(self):
        self.publish(qps=0)
        snapshot = json.loads(self.redis.get(tps_control.BULK_RATE_KEY))
        snapshot["db_ready"] = False
        self.redis.set(tps_control.BULK_RATE_KEY, json.dumps(snapshot))
        self.assertFalse(tps_control.bulk_ingest_status(self.redis)["rate_available"])


if __name__ == "__main__":
    unittest.main()
