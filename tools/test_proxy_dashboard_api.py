#!/usr/bin/env python3
"""
自动化测试 IP 代理配置与集群管理 API 及面板集成
"""

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))

from proxy_pool import (
    load_proxy_config,
    save_proxy_config,
    mask_proxy_config,
    ProxyManager,
    REDIS_PROXY_CONFIG_KEY,
)
from tps_control import (
    find_cluster_pids,
    cluster_status,
)


class MockRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, val):
        self.store[key] = val

    def delete(self, key):
        self.store.pop(key, None)

    def scan_iter(self, match="*"):
        return []

    def keys(self, match="*"):
        return []

    def llen(self, key):
        return 0


class TestProxyDashboardApi(unittest.TestCase):

    def setUp(self):
        self.r = MockRedis()

    def test_save_and_load_proxy_config(self):
        cfg = {
            "mode": "tunnel",
            "tunnel": "http://myuser:secret123@gate.proxy.io:8000",
            "sticky_requests": 25,
            "cooldown_sec": 45.0,
        }
        saved = save_proxy_config(self.r, cfg)
        self.assertEqual(saved["mode"], "tunnel")
        self.assertEqual(saved["tunnel"], "http://myuser:secret123@gate.proxy.io:8000")
        self.assertEqual(saved["sticky_requests"], 25)

        # 检查 load
        loaded = load_proxy_config(self.r)
        self.assertEqual(loaded["tunnel"], "http://myuser:secret123@gate.proxy.io:8000")

        # 检查脱敏
        masked = mask_proxy_config(loaded)
        self.assertEqual(masked["tunnel_masked"], "http://myuser:****@gate.proxy.io:8000")
        self.assertEqual(masked["host"], "gate.proxy.io")
        self.assertEqual(masked["port"], 8000)
        self.assertEqual(masked["username"], "myuser")
        self.assertTrue(masked["has_password"])

    def test_proxy_manager_from_redis(self):
        cfg = {
            "mode": "tunnel",
            "tunnel": "http://myuser:mypass@1.2.3.4:9999",
            "sticky_requests": 15,
        }
        save_proxy_config(self.r, cfg)
        mgr = ProxyManager.from_redis(self.r)
        self.assertEqual(mgr.tunnel, "http://myuser:mypass@1.2.3.4:9999")
        self.assertEqual(mgr.sticky_requests, 15)

    def test_proxy_manager_hot_reload(self):
        cfg1 = {"mode": "direct"}
        save_proxy_config(self.r, cfg1)
        mgr = ProxyManager.from_redis(self.r)
        self.assertIsNone(mgr.tunnel)

        # 模拟后台更新为隧道代理
        cfg2 = {"mode": "tunnel", "tunnel": "http://user:pass@10.0.0.1:8888"}
        save_proxy_config(self.r, cfg2)

        # 检测并热重载
        reloaded = mgr.check_and_reload(self.r)
        self.assertTrue(reloaded)
        self.assertEqual(mgr.tunnel, "http://user:pass@10.0.0.1:8888")

    def test_cluster_status_structure(self):
        status = cluster_status(self.r)
        self.assertIn("running", status)
        self.assertIn("total_qps", status)
        self.assertIn("target_qps", status)
        self.assertIn("target_progress_pct", status)
        self.assertIn("buffer_depth", status)
        self.assertIn("logs", status)
        self.assertEqual(status["target_qps"], 347.2)

    def test_flask_app_routes_registered(self):
        from dashboard_api import app
        rules = [rule.rule for rule in app.url_map.iter_rules()]
        self.assertIn("/api/proxy/config", rules)
        self.assertIn("/api/proxy/test", rules)
        self.assertIn("/api/cluster/status", rules)
        self.assertIn("/api/cluster/control", rules)

    def test_flask_proxy_config_endpoint(self):
        from dashboard_api import app
        client = app.test_client()

        # GET
        res = client.get("/api/proxy/config")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        self.assertIn("config", data)

        # POST 更新配置
        post_data = {
            "mode": "tunnel",
            "host": "gate.abcproxy.com",
            "port": "9000",
            "username": "tester",
            "password": "pwd",
            "sticky_requests": 30,
        }
        res_post = client.post("/api/proxy/config", json=post_data)
        self.assertEqual(res_post.status_code, 200)
        saved_data = res_post.get_json()
        self.assertTrue(saved_data.get("ok"))
        cfg = saved_data.get("config")
        self.assertEqual(cfg.get("mode"), "tunnel")
        self.assertEqual(cfg.get("host"), "gate.abcproxy.com")
        self.assertEqual(cfg.get("port"), 9000)
        self.assertEqual(cfg.get("username"), "tester")
        self.assertEqual(cfg.get("tunnel_masked"), "http://tester:****@gate.abcproxy.com:9000")


if __name__ == "__main__":
    unittest.main()
