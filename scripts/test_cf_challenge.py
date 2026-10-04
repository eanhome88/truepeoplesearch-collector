#!/usr/bin/env python3
"""挑战分型 + 路由回归：死因判对、路由最便宜优先、cap 过滤、计数容错。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cf_challenge as cc


class FakeHttpError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or f"HTTP {status}")
        self.status = status


class ClassifyTests(unittest.TestCase):
    def test_turnstile(self):
        html = '<div class="cf-turnstile" data-sitekey="x"></div>just a moment'
        self.assertEqual(cc.classify(html=html, status=403), cc.TURNSTILE)

    def test_managed_just_a_moment(self):
        self.assertEqual(
            cc.classify(html="<title>Just a moment...</title>checking your browser", status=403),
            cc.MANAGED,
        )

    def test_site_captcha_beats_status(self):
        # 站内验证常伴随 302/200，必须优先于其他判断
        self.assertEqual(
            cc.classify(url="https://x/InternalCaptcha?x=1", html="<html>captcha</html>", status=200),
            cc.SITE_CAPTCHA,
        )

    def test_rate_limit_status_first(self):
        self.assertEqual(cc.classify(html="whatever", status=429), cc.RATE_LIMIT)
        self.assertEqual(
            cc.classify(html="TOO_MANY_REQUESTS", status=200), cc.RATE_LIMIT
        )

    def test_ip_block_needs_403_plus_marker(self):
        html = "<title>Attention Required! | Cloudflare</title>error code: 1020 access denied"
        self.assertEqual(cc.classify(html=html, status=403), cc.IP_BLOCK)
        # 无标记裸 403 不冤枉成封禁
        self.assertEqual(cc.classify(html="<html>forbidden</html>", status=403), cc.UNKNOWN_FAIL)

    def test_origin_5xx(self):
        self.assertEqual(cc.classify(html="<html>bad gateway</html>", status=502), cc.ORIGIN_FAIL)

    def test_empty_200_body(self):
        self.assertEqual(cc.classify(html="   ", status=200), cc.EMPTY_BLOCK)

    def test_clean_and_notfound(self):
        body = "<html><body>John Smith, age 45, Springfield</body></html>"
        self.assertEqual(cc.classify(html=body, status=200), cc.CLEAN)
        self.assertEqual(cc.classify(html="<html>gone</html><p>removed</p><!-- pad pad pad -->", status=410), cc.CLEAN)

    def test_timeout_exception(self):
        self.assertEqual(
            cc.classify_exception(RuntimeError("net::ERR_TIMED_OUT"), "http://x"), cc.TIMEOUT
        )

    def test_empty_response_exception(self):
        self.assertEqual(
            cc.classify_exception(RuntimeError("net::ERR_EMPTY_RESPONSE"), "http://x"),
            cc.EMPTY_BLOCK,
        )

    def test_429_exception(self):
        self.assertEqual(
            cc.classify_exception(FakeHttpError(429, "HTTP 429 captcha"), "http://x"),
            cc.RATE_LIMIT,
        )


class RouteTests(unittest.TestCase):
    def test_turnstile_never_rotates_first(self):
        caps = {"browser": True, "warmed": True, "gateway": False, "solver": False}
        plan = cc.route_for(cc.TURNSTILE, caps)
        # 没网关没求解器时退回浏览器等/退避，绝不先换 IP（换 IP 解不了 Turnstile）
        self.assertNotIn(cc.ROTATE_PROXY, plan[:1])
        self.assertEqual(plan[0], cc.WARMED_RETRY)

    def test_site_captcha_skips_paid_solvers(self):
        caps = {"browser": True, "warmed": True, "gateway": True, "solver": True}
        plan = cc.route_for(cc.SITE_CAPTCHA, caps)
        self.assertNotIn(cc.GATEWAY, plan)
        self.assertNotIn(cc.SOLVER, plan)
        self.assertEqual(plan[0], cc.ROTATE_PROXY)

    def test_managed_cheapest_is_browser_wait(self):
        self.assertEqual(cc.first_action(cc.MANAGED, {"browser": True}), cc.BROWSER_WAIT)

    def test_gateway_used_when_available(self):
        caps = {"browser": True, "warmed": False, "gateway": True, "solver": False}
        self.assertIn(cc.GATEWAY, cc.route_for(cc.TURNSTILE, caps))

    def test_unknown_kind_falls_back(self):
        # 未知死因按代理层问题处理：换组重试优先，退避兜底
        self.assertEqual(cc.first_action("no_such_kind", {"browser": True}), cc.RETRY_OTHER_GROUP)


class CounterTests(unittest.TestCase):
    def test_note_and_snapshot(self):
        store = {}

        class FakeRedis:
            def hincrby(self, key, field, n):
                store[(key, field)] = store.get((key, field), 0) + n

            def hgetall(self, key):
                return {f.encode(): str(v).encode() for (k, f), v in store.items() if k == key}

        r = FakeRedis()
        cc.note_challenge(r, cc.TURNSTILE)
        cc.note_challenge(r, cc.TURNSTILE)
        cc.note_challenge(r, "bogus-kind")
        snap = cc.challenge_snapshot(r)
        self.assertEqual(snap.get(cc.TURNSTILE), 2)
        self.assertEqual(snap.get(cc.UNKNOWN_FAIL), 1)

    def test_redis_down_is_silent(self):
        class DeadRedis:
            def hincrby(self, *a, **k):
                raise ConnectionError("down")

        cc.note_challenge(DeadRedis(), cc.MANAGED)  # 不抛
        self.assertEqual(cc.challenge_snapshot(DeadRedis()), {})


class WorkerWireTests(unittest.TestCase):
    def test_tab_job_reports_kind(self):
        import asyncio

        import distributed_worker as dw
        self.assertIsNotNone(dw.cf_challenge)

        box = mock.MagicMock()
        box.generation = 0
        box.served = 0

        async def boom(_box, _url):
            # 站内验证页（无 429 状态码）：分型应直指 site_captcha，路由换出口
            raise RuntimeError("InternalCaptcha challenge for http://x/find/phone/2015550123")

        job = {"id": "j1", "url": "http://x/InternalCaptcha"}
        with mock.patch.object(dw, "_fetch_page", side_effect=boom), \
             mock.patch.object(dw, "_looks_like_captcha", return_value=False), \
             mock.patch.object(dw, "_has_captcha", return_value=False):
            out = asyncio.run(dw._tab_job(0, box, job))
        # InternalCaptcha 必须分型为站内验证，且首选动作是换出口（网关/求解器解不了站内验证）
        self.assertEqual(out.get("cf_kind"), "site_captcha")
        self.assertEqual(out.get("cf_route"), "rotate_proxy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
