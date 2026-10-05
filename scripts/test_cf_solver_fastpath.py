#!/usr/bin/env python3
"""solver 站内 captcha 快路径单测：只测 scripts/cf_solver.py 新增纯函数，不起浏览器、不出网。

约定（照抄 test_cf_solver_flare.py）：全内存 mock，无网络、无 Redis、无浏览器。
覆盖：见 site_captcha 直接换出口（不触发 resolve 重解），仅 turnstile/managed 才走重解；
flaresolverr:/byparr: 成功判定、无 sessions 等契约不动（末尾锁死一条）。
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cf_solver import (
    CF_RESOLVE_KINDS,
    CfSolver,
    _same_site,
    _split_proxy_auth,
    classify_for_fastpath,
    fastpath_action,
    needs_cf_resolve,
    should_resolve_challenge,
)

SITE_HTML = "<html><body>InternalCaptcha please verify</body></html>" + "x" * 200
TURNSTILE_HTML = '<div class="cf-turnstile" data-sitekey="x"></div>' + "y" * 200
MANAGED_HTML = "<title>Just a moment...</title>checking your browser" + "z" * 200


class NeedsResolveTests(unittest.TestCase):
    def test_site_captcha_never_resolves(self):
        self.assertFalse(needs_cf_resolve("site_captcha"))

    def test_turnstile_and_managed_resolve(self):
        self.assertTrue(needs_cf_resolve("turnstile"))
        self.assertTrue(needs_cf_resolve("managed"))
        self.assertEqual(CF_RESOLVE_KINDS, frozenset({"turnstile", "managed"}))

    def test_other_kinds_do_not_resolve(self):
        for kind in ("rate_limit", "ip_block", "origin_fail", "empty_block",
                     "timeout", "clean", "unknown_fail", "no_such_kind"):
            self.assertFalse(needs_cf_resolve(kind), kind)

    def test_kind_normalization(self):
        self.assertTrue(needs_cf_resolve("  Turnstile "))
        self.assertTrue(needs_cf_resolve("MANAGED"))
        self.assertFalse(needs_cf_resolve("  Site_Captcha "))

    def test_non_string_kind_is_rotate(self):
        self.assertFalse(needs_cf_resolve(None))
        self.assertFalse(needs_cf_resolve(""))
        self.assertFalse(needs_cf_resolve("   "))
        self.assertFalse(needs_cf_resolve(123))


class FastpathActionTests(unittest.TestCase):
    def test_site_captcha_action_is_rotate_proxy(self):
        self.assertEqual(fastpath_action("site_captcha"), "rotate_proxy")

    def test_turnstile_managed_action_is_solver(self):
        self.assertEqual(fastpath_action("turnstile"), "solver")
        self.assertEqual(fastpath_action("managed"), "solver")

    def test_unknown_action_is_rotate_proxy(self):
        self.assertEqual(fastpath_action("unknown_fail"), "rotate_proxy")
        self.assertEqual(fastpath_action(None), "rotate_proxy")


class ClassifyForFastpathTests(unittest.TestCase):
    def test_classify_site_captcha_via_url(self):
        self.assertEqual(
            classify_for_fastpath(url="https://x/InternalCaptcha?u=1",
                                  html="<html>captcha</html>", status=200),
            "site_captcha",
        )

    def test_classify_turnstile(self):
        self.assertEqual(
            classify_for_fastpath(html=TURNSTILE_HTML, status=403), "turnstile")

    def test_classify_managed(self):
        self.assertEqual(
            classify_for_fastpath(html=MANAGED_HTML, status=403), "managed")

    def test_classify_fallback_without_cf_challenge(self):
        with mock.patch.dict(sys.modules, {"cf_challenge": None}):
            self.assertEqual(
                classify_for_fastpath(url="https://x/InternalCaptcha",
                                      html="<html>hi</html>"), "site_captcha")
            self.assertEqual(
                classify_for_fastpath(html=TURNSTILE_HTML), "turnstile")
            self.assertEqual(
                classify_for_fastpath(html=MANAGED_HTML), "managed")


class ShouldResolveTests(unittest.TestCase):
    def test_should_resolve_explicit_kind_wins(self):
        # 显式 site_captcha 即使正文像 Turnstile 也不重解
        self.assertFalse(should_resolve_challenge(html=TURNSTILE_HTML, kind="site_captcha"))
        self.assertTrue(should_resolve_challenge(html=SITE_HTML, kind="turnstile"))

    def test_should_resolve_classifies_site_captcha_html(self):
        self.assertFalse(should_resolve_challenge(
            url="https://www.truepeoplesearch.com/InternalCaptcha",
            html=SITE_HTML, status=200))

    def test_should_resolve_classifies_turnstile_html(self):
        self.assertTrue(should_resolve_challenge(html=TURNSTILE_HTML, status=403))


class NoWasteAndContractTests(unittest.TestCase):
    def test_site_captcha_skips_solver_call(self):
        calls = []

        async def fake_resolve(url):
            calls.append(url)
            return {"cookies": {"cf_clearance": "x"}}

        kind = classify_for_fastpath(
            url="https://www.truepeoplesearch.com/InternalCaptcha",
            html=SITE_HTML, status=200)
        if should_resolve_challenge(kind=kind):
            asyncio.run(fake_resolve("https://x/InternalCaptcha"))
        self.assertEqual(kind, "site_captcha")
        self.assertEqual(calls, [])

        kind2 = classify_for_fastpath(html=TURNSTILE_HTML, status=403)
        if should_resolve_challenge(kind=kind2):
            asyncio.run(fake_resolve("https://x/turnstile"))
        self.assertEqual(calls, ["https://x/turnstile"])

    def test_flare_byparr_contract_untouched(self):
        self.assertEqual(CfSolver("byparr:http://127.0.0.1:8191/v1")._flavor, "byparr")
        self.assertEqual(CfSolver("flaresolverr:http://127.0.0.1:8191/v1")._flavor, "flaresolverr")
        self.assertFalse(_same_site("https://www.truepeoplesearch.com/a", "https://evil.example.com/z"))
        bare, user, pwd = _split_proxy_auth("http://u1:p1@gw:8080")
        self.assertEqual((bare, user, pwd), ("http://gw:8080", "u1", "p1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
