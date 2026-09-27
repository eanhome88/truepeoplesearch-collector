# -*- coding: utf-8 -*-
"""Unit tests for upgraded Dashboard API (multi-dimensional filters, performance metrics, CSV export)."""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

API_PATH = Path(__file__).resolve().parents[1] / "tools" / "dashboard_api.py"


def load_api():
    spec = importlib.util.spec_from_file_location("dashboard_api_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.dict(os.environ, {}, clear=True):
    api = load_api()


class DashboardEnhancementTests(unittest.TestCase):
    def setUp(self):
        self.app = api.app.test_client()

    def test_build_persons_filter_all_dimensions(self):
        args = {
            "search": "David",
            "phone": "(201) 555-1234",
            "city": "Jersey City",
            "state": "nj",
            "phone_type": "Wireless",
            "has_wireless": "1",
            "age_min": "30",
            "age_max": "65",
            "sort": "name_asc",
        }
        where, params, order_sql = api._build_persons_filter(args)
        
        # Verify conditions
        where_joined = " AND ".join(where)
        self.assertIn("p.full_name LIKE %s", where_joined)
        self.assertIn("p.primary_phone LIKE %s", where_joined)
        self.assertIn("p.current_city = %s", where_joined)
        self.assertIn("p.current_state = %s", where_joined)
        self.assertIn("p.primary_phone_type = %s", where_joined)
        self.assertIn("p.wireless_phone_1 IS NOT NULL", where_joined)
        self.assertIn("p.age >= %s", where_joined)
        self.assertIn("p.age <= %s", where_joined)
        self.assertEqual(order_sql, "ORDER BY p.full_name ASC, p.person_id ASC")
        
        # Verify state is capitalized to NJ
        self.assertIn("NJ", params)
        # Verify cleaned phone number digits are passed
        self.assertIn("%2015551234%", params)

    @patch.object(api, "query_one")
    @patch.object(api, "get_redis")
    def test_load_stats_performance_metrics(self, mock_get_redis, mock_query_one):
        # Mock DB row
        mock_query_one.return_value = {
            "persons": 1000,
            "phones": 2500,
            "emails": 800,
            "prev_addr": 1500,
            "aliases": 300,
            "wireless_count": 850,
            "primary_wireless_count": 850,
            "smart_fallback_count": 120,
        }
        
        # Mock Redis & metrics
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis
        
        with patch("tps_metrics.get_metrics") as mock_get_metrics, \
             patch("tps_queue.queue_stats") as mock_queue_stats, \
             patch("tps_control._scan_heartbeats") as mock_workers:
            
            mock_metrics_inst = MagicMock()
            mock_metrics_inst.snapshot.return_value = {
                "counters": {
                    "attempt": 1500,
                    "success": 1495,
                    "dedup_hit": 300,
                },
                "latency": {
                    "scrape_ms": {"avg": 42.5},
                },
            }
            mock_get_metrics.return_value = mock_metrics_inst
            mock_queue_stats.return_value = {"pending": 50, "processing": 32, "total": 1500}
            mock_workers.return_value = [{"current_qps": 16.0}, {"current_qps": 16.5}]

            stats = api._load_stats()
            
            # Check core performance indicators
            self.assertEqual(stats["persons"], 1000)
            self.assertEqual(stats["wireless_count"], 850)
            self.assertEqual(stats["smart_fallback_count"], 120)
            self.assertEqual(stats["wireless_ratio_pct"], 85.0)
            
            self.assertEqual(stats["total_tasks_executed"], 1500)
            self.assertEqual(stats["success_tasks"], 1495)
            self.assertGreaterEqual(stats["success_rate_pct"], 99.0)
            self.assertGreater(stats["traffic_saved_mb"], 3000.0)
            self.assertGreater(stats["traffic_saved_gb"], 3.0)
            self.assertEqual(stats["avg_latency_ms"], 42.5)
            self.assertEqual(stats["current_qps"], 32.5)

    @patch.object(api, "query")
    def test_export_csv_utf8_bom_and_headers(self, mock_query):
        # Mock database rows
        mock_query.return_value = [
            {
                "人物ID": "px101",
                "全名": "John Michael Doe",
                "性别": "男",
                "年龄": 45,
                "当前电话": "(201) 555-1234",
                "当前电话类型": "Wireless",
                "当前地址": "123 Main St, Jersey City, NJ 07302",
                "当前地址时长": "(Jan 2018 - Present)",
                "姓": "Doe",
                "名": "John",
                "中间名": "Michael",
                "电话列表": "(201) 555-1234, (201) 555-9999",
                "移动号码1": "(201) 555-1234",
                "移动号码2": "(201) 555-9999",
                "移动号码3": "",
            }
        ]

        resp = self.app.get("/api/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers.get("Content-Type", ""))
        self.assertIn("attachment; filename=tps_leads_export_", resp.headers.get("Content-Disposition", ""))

        data = resp.data
        # Must start with UTF-8 BOM
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        
        decoded = data.decode("utf-8-sig")
        lines = [line.strip() for line in decoded.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)
        
        # Check header
        expected_header = "人物ID,全名,性别,年龄,当前电话,当前电话类型,当前地址,当前地址时长,姓,名,中间名,电话列表,移动号码1,移动号码2,移动号码3"
        self.assertEqual(lines[0], expected_header)
        
        # Check row content
        self.assertIn("John Michael Doe", lines[1])
        self.assertIn("(201) 555-1234", lines[1])
        self.assertIn("Wireless", lines[1])


if __name__ == "__main__":
    unittest.main()
