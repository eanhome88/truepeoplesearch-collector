#!/usr/bin/env python3
"""worker 复用延长 bundle 单测：egress 复用检查 / 6~8s 节奏 / 头轮换 / 8~15 页回收。

手写 Fake（FakeProbe / FakeSession / FakeSleep / FakeBox），零 mock 生产模块，
无真实网络、无真实浏览器。只覆盖 scripts/distributed_worker.py 新增行为。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import distributed_worker as w  # noqa: E402
from egress_guard import EgressGuard, expire_own_cf_session_if_egress_moved  # noqa: E402


class FakeProbe:
    """手写 Fake：按脚本返回出口 IP；"RAISE" 表示探测失败。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, proxy_url, timeout):
        self.calls.append((proxy_url, timeout))
        if not self.script:
            return ""
        nxt = self.script.pop(0)
        if nxt == "RAISE":
            raise TimeoutError("fake probe timeout")
        return nxt


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, delay):
        self.calls.append(float(delay))


class FakeBox:
    def __init__(self):
        self.served = 0
        self.generation = 0


class FakeOwnCfSession:
    def __init__(self):
        self.proxy = "http://u:p@10.0.0.1:8000"
        self.expires_at = time.time() + 1500.0

    @property
    def expired(self):
        return bool(self.expires_at) and time.time() >= self.expires_at


class EnvGuard:
    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        return False


class RecycleRangeTests(unittest.TestCase):
    def test_default_range_8_15(self):
        with EnvGuard(TPS_SESSION_RECYCLE=None):
            self.assertEqual(w._parse_session_recycle_range(), (8, 15))

    def test_single_int_env(self):
        with EnvGuard(TPS_SESSION_RECYCLE="12"):
            self.assertEqual(w._parse_session_recycle_range(), (12, 12))

    def test_range_env(self):
        with EnvGuard(TPS_SESSION_RECYCLE="8,15"):
            self.assertEqual(w._parse_session_recycle_range(), (8, 15))

    def test_garbage_falls_back(self):
        with EnvGuard(TPS_SESSION_RECYCLE="bogus"):
            self.assertEqual(w._parse_session_recycle_range(), (8, 15))

    def test_draws_within_8_15(self):
        with EnvGuard(TPS_SESSION_RECYCLE=None):
            for _ in range(200):
                self.assertGreaterEqual(w.draw_session_recycle_limit(), 8)
                self.assertLessEqual(w.draw_session_recycle_limit(), 15)

    def test_new_box_recycle_in_range(self):
        with EnvGuard(TPS_SESSION_RECYCLE=None):
            box = w._ChromeBox(1, proxy="http://u:p@10.0.0.1:8000", lane_id="3")
            self.assertGreaterEqual(box.recycle_at, 8)
            self.assertLessEqual(box.recycle_at, 15)

    def test_recycle_default_not_120(self):
        with EnvGuard(TPS_SESSION_RECYCLE=None):
            self.assertNotEqual(w._parse_session_recycle_range()[1], 120)
            self.assertLessEqual(w.SESSION_RECYCLE_PAGES, 15)


class PageGapTests(unittest.TestCase):
    def test_default_gap_6_8(self):
        with EnvGuard(TPS_PAGE_GAP_SEC=None):
            self.assertEqual(w._parse_page_gap_sec(), (6.0, 8.0))

    def test_gap_draws_in_range(self):
        with EnvGuard(TPS_PAGE_GAP_SEC=None):
            for _ in range(200):
                g = w.page_gap_sec()
                self.assertGreaterEqual(g, 6.0)
                self.assertLessEqual(g, 8.0)

    def test_gap_off(self):
        with EnvGuard(TPS_PAGE_GAP_SEC="0,0"):
            self.assertEqual(w.page_gap_sec(), 0.0)

    def test_gap_garbage_falls_back(self):
        with EnvGuard(TPS_PAGE_GAP_SEC="bogus"):
            self.assertEqual(w._parse_page_gap_sec(), (6.0, 8.0))


class TabJobPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_tab_job_sleeps_6_8s(self):
        with EnvGuard(TPS_PAGE_GAP_SEC=None):
            box = FakeBox()
            sleep = FakeSleep()
            res = await w._tab_job(0, box, {}, sleep_fn=sleep)
            self.assertEqual(res["bucket"], "parse_fail")
            self.assertEqual(len(sleep.calls), 1)
            self.assertGreaterEqual(sleep.calls[0], 6.0)
            self.assertLessEqual(sleep.calls[0], 8.0)

    async def test_tab_job_gap_off_no_sleep(self):
        with EnvGuard(TPS_PAGE_GAP_SEC="0,0"):
            box = FakeBox()
            sleep = FakeSleep()
            await w._tab_job(0, box, {}, sleep_fn=sleep)
            self.assertEqual(sleep.calls, [])


class HeaderTests(unittest.TestCase):
    def test_headers_rotate_per_page(self):
        refs, langs = set(), set()
        for _ in range(300):
            h = w.build_page_headers("https://www.truepeoplesearch.com/find/person/x")
            refs.add(h["referer"])
            langs.add(h["accept-language"])
        self.assertGreater(len(refs), 1)
        self.assertGreater(len(langs), 1)

    def test_headers_browser_consistent(self):
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        h = w.build_page_headers("https://www.truepeoplesearch.com/find/person/x", ua)
        for key in ("user-agent", "accept", "accept-language", "referer",
                    "upgrade-insecure-requests", "sec-fetch-site", "sec-fetch-mode",
                    "sec-fetch-user", "sec-fetch-dest", "sec-ch-ua",
                    "sec-ch-ua-mobile", "sec-ch-ua-platform"):
            self.assertIn(key, h)
        self.assertEqual(h["user-agent"], ua)
        self.assertIn("124", h["sec-ch-ua"])
        self.assertIn(h["sec-fetch-site"], ("same-origin", "cross-site", "none"))
        self.assertEqual(
            h["sec-fetch-site"] == "same-origin",
            "truepeoplesearch.com" in (h["referer"] or ""),
        )


class EgressReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_egress_keeps_session(self):
        guard = EgressGuard(probe=FakeProbe(["1.1.1.1", "1.1.1.1"]))
        box = w._ChromeBox(1, proxy="http://u:p@10.0.0.1:8000",
                           lane_id="7", egress_guard=guard, recycle_at=15)
        sess = FakeSession()
        box.session = sess
        box.served = 3
        calls = []

        async def _opener():
            calls.append(1)
            return FakeSession()

        out = await box.ensure_session(opener=_opener)
        self.assertIs(out, sess)
        self.assertFalse(sess.closed)
        self.assertEqual(calls, [])

    async def test_drift_expires_session(self):
        guard = EgressGuard(probe=FakeProbe(["1.1.1.1", "2.2.2.2"]))
        box = w._ChromeBox(1, proxy="http://u:p@10.0.0.1:8000",
                           lane_id="7", egress_guard=guard, recycle_at=15)
        sess = FakeSession()
        box.session = sess
        box.served = 3

        async def _opener():
            return FakeSession()

        primed = await box.ensure_session(opener=_opener)
        self.assertIs(primed, sess)
        out = await box.ensure_session(opener=_opener)
        self.assertTrue(sess.closed)
        self.assertIsNot(out, sess)
        self.assertEqual(box.served, 0)

    async def test_probe_failure_keeps_session(self):
        guard = EgressGuard(probe=FakeProbe(["1.1.1.1", "RAISE"]))
        box = w._ChromeBox(1, proxy="http://u:p@10.0.0.1:8000",
                           lane_id="7", egress_guard=guard, recycle_at=15)
        sess = FakeSession()
        box.session = sess
        box.served = 3
        await box.ensure_session(opener=lambda: FakeSession())
        self.assertIs(box.session, sess)
        self.assertFalse(sess.closed)

    async def test_recycle_limit_triggers_new_session(self):
        guard = EgressGuard(probe=FakeProbe(["9.9.9.9"] * 4))
        box = w._ChromeBox(1, proxy="http://u:p@10.0.0.1:8000",
                           lane_id="7", egress_guard=guard, recycle_at=10)
        sess = FakeSession()
        box.session = sess
        box.served = 10
        fresh = FakeSession()

        async def _opener():
            return fresh

        out = await box.ensure_session(opener=_opener)
        self.assertTrue(sess.closed)
        self.assertIs(out, fresh)
        self.assertEqual(box.served, 0)
        self.assertGreaterEqual(box.recycle_at, 8)
        self.assertLessEqual(box.recycle_at, 15)

    def test_own_cf_drift_marks_expired(self):
        guard = EgressGuard(probe=FakeProbe(["1.1.1.1", "5.6.7.8"]))
        proxy = "http://u:p@10.0.0.1:8000"
        self.assertFalse(guard.check_lane("7", proxy))
        sess = FakeOwnCfSession()
        self.assertFalse(sess.expired)
        moved = expire_own_cf_session_if_egress_moved("7", proxy, sess, guard)
        self.assertTrue(moved)
        self.assertTrue(sess.expired)


if __name__ == "__main__":
    unittest.main(verbosity=2)
