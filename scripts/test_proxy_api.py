#!/usr/bin/env python3
"""Upstream proxy pull (PROXY_API_URL) and hot lane merge."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_pool import StickyLanes, fetch_proxy_list  # noqa: E402


def _temp_file(content: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".txt", delete=False,
    )
    handle.write(content)
    handle.close()
    return Path(handle.name).as_uri()


class TestFetchProxyList(unittest.TestCase):
    def test_plain_text_lines(self):
        url = _temp_file(
            "# comment\n"
            "\n"
            "http://u1:p1@10.0.0.1:8080\n"
            "10.0.0.2:8080\n"
            "http://u1:p1@10.0.0.1:8080\n"
        )
        self.assertEqual(
            fetch_proxy_list(url),
            ["http://u1:p1@10.0.0.1:8080", "http://10.0.0.2:8080"],
        )

    def test_json_shapes(self):
        url = _temp_file(
            '{"data": ["http://a:1", {"host": "h", "port": 8080, '
            '"user": "u", "pass": "p"}], "other": 1}'
        )
        self.assertEqual(
            fetch_proxy_list(url),
            ["http://a:1", "http://u:p@h:8080"],
        )

    def test_json_bare_list(self):
        url = _temp_file('["10.0.0.9:9000"]')
        self.assertEqual(fetch_proxy_list(url), ["http://10.0.0.9:9000"])

    def test_empty_means_error(self):
        url = _temp_file("# nothing here\n\n")
        with self.assertRaises(RuntimeError):
            fetch_proxy_list(url)

    def test_unreachable_means_error_without_leaking_url(self):
        with self.assertRaises(RuntimeError) as ctx:
            fetch_proxy_list("http://user:secret@127.0.0.1:9/nope", timeout=5)
        self.assertNotIn("secret", str(ctx.exception))


class TestMergeUrls(unittest.TestCase):
    def test_merge_adds_only_new_and_rested(self):
        lanes = StickyLanes(["http://10.0.0.1:8000", "http://10.0.0.2:8000"])
        added = lanes.merge_urls(["http://10.0.0.1:8000", "http://10.0.0.3:8000"])
        self.assertEqual(added, 1)
        self.assertEqual(lanes.count, 3)
        self.assertFalse(lanes.shared_host)
        nxt = lanes.cool("holder-new")
        self.assertEqual(nxt, "http://10.0.0.1:8000")

    def test_merge_shared_host_detection(self):
        lanes = StickyLanes(["http://u@ gw.example:1 ".replace(" ", "")])
        self.assertTrue(lanes.shared_host)
        added = lanes.merge_urls(["http://v@gw.example:2"])
        self.assertEqual(added, 1)
        self.assertTrue(lanes.shared_host)


class TestLoadWorkerLanesFromApi(unittest.TestCase):
    def test_api_builds_lanes_without_redis(self):
        from distributed_worker import load_worker_lanes

        url = _temp_file("http://u1:p1@10.0.0.1:8080\n10.0.0.2:8080\n")
        cache = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        cache.close()
        import os

        os.environ["PROXY_API_CACHE"] = cache.name
        self.addCleanup(os.environ.pop, "PROXY_API_CACHE", None)
        self.addCleanup(os.unlink, cache.name)
        lanes = load_worker_lanes(proxy_api_url=url)
        self.assertIsNotNone(lanes)
        self.assertEqual(lanes.count, 2)
        self.assertFalse(lanes.shared_host)


if __name__ == "__main__":
    unittest.main(verbosity=2)
