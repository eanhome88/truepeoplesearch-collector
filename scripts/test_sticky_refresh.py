#!/usr/bin/env python3
"""刷新 region 网关粘性 sid，已有 sid 换掉而不是再拼一段。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_pool import refresh_sticky_url  # noqa: E402


FRESH = "http://acct-region-US:secret@gate.example:5000"
STALE = "http://acct-region-US-sid-abc-t-30:secret@gate.example:5000"
PLAIN = "http://acct:secret@gate.example:5000"


def _sid(url: str) -> str:
    user = urlparse(url).username or ""
    head, sep, tail = user.partition("-sid-")
    if not sep or "-sid-" in tail:
        raise AssertionError(f"expected one sid segment in {url!r}")
    sid, sep, _rest = tail.partition("-t-")
    if not sep or not sid:
        raise AssertionError(f"expected sid and hold in {url!r}")
    return sid


class TestRefreshStickyUrl(unittest.TestCase):
    def test_fresh_region_url_gets_sid_and_keeps_secret(self):
        url = refresh_sticky_url(FRESH)
        self.assertIn("-sid-", url)
        self.assertIn("-t-120", url)
        self.assertIn("acct-region-US", url)
        self.assertIn("gate.example:5000", url)
        self.assertIn("secret", url)
        parsed = urlparse(url)
        self.assertEqual(parsed.password, "secret")
        self.assertEqual(parsed.hostname, "gate.example")
        self.assertEqual(parsed.port, 5000)
        self.assertTrue((parsed.username or "").startswith("acct-region-US-sid-"))
        self.assertTrue((parsed.username or "").endswith("-t-120"))

    def test_two_calls_get_different_sids(self):
        first = refresh_sticky_url(FRESH)
        second = refresh_sticky_url(FRESH)
        self.assertNotEqual(_sid(first), _sid(second))

    def test_existing_sid_is_replaced_not_appended(self):
        url = refresh_sticky_url(STALE)
        self.assertEqual(url.count("-sid-"), 1)
        self.assertIn("-t-120", url)
        self.assertNotIn("-t-30", url)
        self.assertNotIn("-sid-abc", url)
        self.assertNotEqual(_sid(url), "abc")
        self.assertIn("acct-region-US", url)
        self.assertIn("gate.example:5000", url)
        self.assertEqual(urlparse(url).password, "secret")

    def test_url_without_region_is_unchanged(self):
        self.assertEqual(refresh_sticky_url(PLAIN), PLAIN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
