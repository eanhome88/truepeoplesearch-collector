#!/usr/bin/env python3
"""隐身四件套回归：referer 轮换 / init 脚本 / locale 时区 / 抖动解析。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrape_to_tidb as s


class RefererTests(unittest.TestCase):
    def test_google_mode(self):
        os.environ["TPS_REFERER_MODE"] = "google"
        try:
            self.assertEqual(s.pick_referer(), "https://www.google.com/")
        finally:
            os.environ.pop("TPS_REFERER_MODE", None)

    def test_none_mode(self):
        os.environ["TPS_REFERER_MODE"] = "none"
        try:
            self.assertEqual(s.pick_referer(), "")
        finally:
            os.environ.pop("TPS_REFERER_MODE", None)

    def test_rotate_mix(self):
        os.environ.pop("TPS_REFERER_MODE", None)
        refs = [s.pick_referer("https://www.truepeoplesearch.com/find/person/x") for _ in range(600)]
        internal = sum(1 for r in refs if "truepeoplesearch.com" in r)
        search = sum(1 for r in refs if "google.com" in r or "bing.com" in r or "yahoo.com" in r)
        direct = sum(1 for r in refs if r == "")
        # 权重 70/20/10，大样本下允许 ±10 个点漂移
        self.assertGreater(internal, 300)
        self.assertGreater(search, 50)
        self.assertGreater(direct, 20)
        # 不再是 100% Google
        self.assertLess(sum(1 for r in refs if r == "https://www.google.com/"), 600)

    def test_fetch_kwargs_per_request_referer(self):
        os.environ.pop("TPS_REFERER_MODE", None)
        kw = s.fetch_kwargs("https://www.truepeoplesearch.com/find/person/x")
        self.assertFalse(kw.get("google_search"))
        self.assertIn("referer", (kw.get("extra_headers") or {}))


class SessionProfileTests(unittest.TestCase):
    def test_no_site_wide_google_referer(self):
        self.assertFalse(s.session_kwargs().get("google_search"))

    def test_no_placebo_init_script(self):
        # E2E 结论：patchright 静默吞掉自定义注入，挂 init_script 等于假隐身。
        # 这里锁死“不挂”，将来换栈（原生 playwright）时再恢复。
        self.assertNotIn("init_script", s.session_kwargs())

    def test_locale_timezone_aligned_us(self):
        for var in ("TPS_LOCALE", "TPS_TIMEZONE"):
            os.environ.pop(var, None)
        kw = s.session_kwargs()
        self.assertEqual(kw.get("locale"), "en-US")
        self.assertEqual(kw.get("timezone_id"), "America/New_York")


class JitterTests(unittest.TestCase):
    def test_default_and_off_and_garbage(self):
        os.environ.pop("TPS_FETCH_JITTER_MS", None)
        self.assertEqual(s.parse_jitter_ms(), (200, 800))
        os.environ["TPS_FETCH_JITTER_MS"] = "0,0"
        try:
            self.assertEqual(s.parse_jitter_ms(), (0, 0))
        finally:
            os.environ.pop("TPS_FETCH_JITTER_MS", None)
        os.environ["TPS_FETCH_JITTER_MS"] = "bogus"
        try:
            self.assertEqual(s.parse_jitter_ms(), (200, 800))
        finally:
            os.environ.pop("TPS_FETCH_JITTER_MS", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
