#!/usr/bin/env python3
"""feed_discovery 单测：手写 Fake，全内存，无网络、无浏览器、无落盘（除 tmp_path）。

覆盖：
- sitemap 索引纯解析：只收 names-<字母>-<序号>.xml.gz（合成 315 个+噪声）；
- names 子文件纯解析：只收 /find/<姓>/<名>，排除人物页/字母页/站外；
- 目录页只认卡片 data-detail-link，普通 href 人物链不算，非 19 位 ID 不要；
- 代理池只读 71-73 行；日志打码不带密码；
- 硬钳：总请求<=60、间隔 floor 3 秒；失败即记不重试；
- solver lane 请求体：byparr 风味（代理走 X-Proxy-* 头，body 无代理）；
- ID 去重 + 追加落盘（已存在的不重复写）。
"""
import gzip
import io
import json
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from feed_discovery import (
    MAX_REQUESTS,
    MIN_INTERVAL_SEC,
    SolvedLaneFetcher,
    extract_person_ids_from_directory_html,
    load_pool_proxies,
    mask_proxy,
    parse_names_sitemap,
    parse_sitemap_index,
    person_page_urls,
    run_discovery,
)

PID_A = "a" * 19
PID_B = "B2c4D6e8F0h1J3k5M7x"  # 19 位
PID_C = "p6uu4ru9u4r4946402un"  # 20 位（线上实测卡片 ID 18–21 位不等）
PID_SHORT = "abc123"  # 非 ID 短串，不要


def _dir_html(*pids):
    cards = "".join(
        f'<div class="card" data-detail-link="/find/person/{p}">x</div>'
        for p in pids
    )
    return (
        "<html><body>"
        + cards
        + f'<a href="/find/person/{PID_A}">plain href not counted</a>'
        + f'<div data-detail-link="/find/person/{PID_SHORT}">short</div>'
        + "</body></html>"
    )


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, sec):
        self.calls.append(sec)


class FakeFetch:
    """手写假 lane：按 URL 给 html 或抛错，记调用（单次尝试，不重试）。"""

    def __init__(self, pages=None, fail_urls=()):
        self.pages = dict(pages or {})
        self.fail_urls = set(fail_urls)
        self.calls = []  # (url, proxy)

    def __call__(self, url, proxy=""):
        self.calls.append((url, proxy))
        if url in self.fail_urls:
            raise RuntimeError("PerimeterX 429")
        return self.pages.get(url, "<html></html>")


class FakeHTTPResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """假 solver /v1：记 body/headers，永远回干净解。"""

    def __init__(self):
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append({
            "url": req.full_url,
            "body": json.loads(req.data.decode("utf-8")),
            "headers": dict(req.headers),
            "timeout": timeout,
        })
        doc = {"status": "ok", "solution": {
            "cookies": [{"name": "cf_clearance", "value": "c"}],
            "userAgent": "UA",
            "url": self.calls[-1]["body"]["url"],
            "response": "<html>" + "z" * 100 + "</html>",
        }}
        return FakeHTTPResp(json.dumps(doc).encode("utf-8"))


class SitemapParseTest(unittest.TestCase):
    def test_index_keeps_only_names_children(self):
        locs = [f"https://www.truepeoplesearch.com/names-{ch}-{i}.xml.gz"
                for ch in ("a", "b") for i in range(1, 4)]
        noise = ["https://www.truepeoplesearch.com/sitemap.xml",
                 "https://evil.example.com/names-a-1.xml.gz",
                 "https://www.truepeoplesearch.com/people-1.xml.gz"]
        xml = "<sitemapindex>" + "".join(f"<sitemap><loc>{u}</loc></sitemap>"
                                         for u in locs + noise) + "</sitemapindex>"
        self.assertEqual(parse_sitemap_index(xml), locs)

    def test_index_scales_to_315(self):
        locs = [f"https://www.truepeoplesearch.com/names-{chr(97 + (i % 26))}-{i}.xml.gz"
                for i in range(315)]
        xml = "".join(f"<loc>{u}</loc>" for u in locs)
        self.assertEqual(len(parse_sitemap_index(xml)), 315)

    def test_names_sitemap_keeps_two_segment_dirs(self):
        xml = ("<urlset>"
               "<url><loc>https://www.truepeoplesearch.com/find/smith/john</loc></url>"
               "<url><loc>https://www.truepeoplesearch.com/find/li/wei</loc></url>"
               "<url><loc>https://www.truepeoplesearch.com/find/person/%s</loc></url>"
               "<url><loc>https://www.truepeoplesearch.com/find/a</loc></url>"
               "<url><loc>https://evil.example.com/find/smith/john</loc></url>"
               "</urlset>") % PID_A
        self.assertEqual(parse_names_sitemap(xml), [
            "https://www.truepeoplesearch.com/find/smith/john",
            "https://www.truepeoplesearch.com/find/li/wei",
        ])

    def test_names_gz_bytes_parse(self):
        raw = ("<urlset><url><loc>https://www.truepeoplesearch.com/find/wang/qi"
               "</loc></url></urlset>").encode()
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(raw)
        text = gzip.decompress(buf.getvalue()).decode("utf-8")
        self.assertEqual(parse_names_sitemap(text),
                         ["https://www.truepeoplesearch.com/find/wang/qi"])


class DirectoryExtractTest(unittest.TestCase):
    def test_only_data_detail_link_counts(self):
        ids = extract_person_ids_from_directory_html(
            _dir_html(PID_A, PID_B, PID_C, PID_A))
        self.assertEqual(ids, [PID_A, PID_B, PID_C])  # 保序去重；href/短串不算

    def test_no_cards_no_ids(self):
        html = f'<html><a href="/find/person/{PID_B}">link</a></html>'
        self.assertEqual(extract_person_ids_from_directory_html(html), [])


class ProxyTest(unittest.TestCase):
    def _pool(self, path, n=80):
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(1, n + 1):
                fh.write(f"http://user{i}:pass{i}@gw{i}.test:8080\n")

    def test_load_lines_71_to_73(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            path = fh.name
        try:
            self._pool(path)
            got = load_pool_proxies(path, 71, 73)
            self.assertEqual(len(got), 3)
            self.assertTrue(got[0].startswith("http://user71:"))
            self.assertTrue(got[2].startswith("http://user73:"))
        finally:
            os.unlink(path)

    def test_load_normalizes_scheme_less_lines(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("u1:p1@gw.test:8080\n")
            path = fh.name
        try:
            got = load_pool_proxies(path, 1, 1)
            self.assertEqual(got, ["http://u1:p1@gw.test:8080"])
        finally:
            os.unlink(path)

    def test_mask_hides_credentials(self):
        masked = mask_proxy("http://user71:pass71@gw71.test:8080")
        self.assertNotIn("pass71", masked)
        self.assertNotIn("user71", masked)
        self.assertIn("gw71.test:8080", masked)
        self.assertEqual(mask_proxy(""), "direct")


class ThrottleTest(unittest.TestCase):
    def test_caps_requests_and_floors_interval(self):
        urls = [f"https://www.truepeoplesearch.com/find/n{i}/m{i}" for i in range(100)]
        fake = FakeFetch({u: _dir_html(PID_A) for u in urls})
        sleep = FakeSleep()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "feed_ids.txt")
            stats = run_discovery(urls, fake, out_path=out,
                                  max_requests=1000, interval_sec=0.5,
                                  sleep=sleep, proxies=("http://u:p@gw:1",))
        self.assertLessEqual(stats["requested"], MAX_REQUESTS)
        self.assertLessEqual(len(fake.calls), 60)
        self.assertTrue(sleep.calls)
        self.assertTrue(all(s >= MIN_INTERVAL_SEC for s in sleep.calls))

    def test_failure_is_recorded_not_retried(self):
        urls = ["https://www.truepeoplesearch.com/find/a/b",
                "https://www.truepeoplesearch.com/find/c/d"]
        fake = FakeFetch({urls[1]: _dir_html(PID_B)}, fail_urls={urls[0]})
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "feed_ids.txt")
            stats = run_discovery(urls, fake, out_path=out,
                                  max_requests=60, interval_sec=3,
                                  sleep=FakeSleep())
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(stats["ok"], 1)
            self.assertEqual(stats["fail_urls"], [urls[0]])
            self.assertEqual(len(fake.calls), 2)  # 失败的不硬试
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(fh.read().split(), [PID_B])

    def test_fresh_ids_are_pushed_to_queue(self):
        pushed = []

        def _push(urls):
            pushed.extend(urls)
            return {"enqueued": len(urls)}

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "feed_ids.txt")
            stats = run_discovery(
                ["https://www.truepeoplesearch.com/find/a/b"],
                FakeFetch({"https://www.truepeoplesearch.com/find/a/b": _dir_html(PID_A)}),
                out_path=out, max_requests=5, interval_sec=3, sleep=FakeSleep(),
                queue_push=_push,
            )
        self.assertEqual(stats["queued"], {"enqueued": 1})
        self.assertEqual(pushed, person_page_urls([PID_A]))

    def test_ids_deduped_against_existing_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "feed_ids.txt")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(PID_A + "\n")
            fake = FakeFetch({"https://u/d": _dir_html(PID_A, PID_B)})
            stats = run_discovery(["https://u/d"], fake, out_path=out,
                                  sleep=FakeSleep())
            self.assertEqual(stats["ids_new"], 1)
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(sorted(fh.read().split()), sorted([PID_A, PID_B]))


class SolverLaneTest(unittest.TestCase):
    def test_byparr_flavor_headers_no_body_proxy(self):
        lane = SolvedLaneFetcher(timeout=45)
        fake = FakeOpener()
        lane._opener = fake
        html = lane.fetch("https://www.truepeoplesearch.com/find/smith/john",
                          "http://u1:p1@gw:8080")
        self.assertIn("z" * 10, html)
        call = fake.calls[0]
        self.assertEqual(call["url"], "http://127.0.0.1:8191/v1")
        self.assertNotIn("proxy", call["body"])
        self.assertEqual(call["body"]["maxTimeout"], 45000)
        headers = {k.lower(): v for k, v in call["headers"].items()}
        self.assertEqual(headers.get("x-proxy-server"), "http://gw:8080")
        self.assertEqual(headers.get("x-proxy-username"), "u1")
        self.assertEqual(headers.get("x-proxy-password"), "p1")

    def test_solver_error_becomes_lane_error(self):
        from feed_discovery import LaneError
        lane = SolvedLaneFetcher()

        class BadOpener:
            def open(self, req, timeout=None):
                return FakeHTTPResp(b'{"status":"error","message":"nope"}')

        lane._opener = BadOpener()
        with self.assertRaises(LaneError):
            lane.fetch("https://www.truepeoplesearch.com/find/a/b")

    def test_captcha_page_becomes_lane_error(self):
        from feed_discovery import LaneError
        lane = SolvedLaneFetcher()
        bad = ("<html><head><title>Captcha</title></head><body>"
               + "y" * 200 + "</body></html>")

        class CaptchaOpener:
            def open(self, req, timeout=None):
                doc = {"status": "ok", "solution": {
                    "cookies": [{"name": "cf_clearance", "value": "c"}],
                    "userAgent": "UA", "url": "u", "response": bad}}
                return FakeHTTPResp(json.dumps(doc).encode("utf-8"))

        lane._opener = CaptchaOpener()
        with self.assertRaises(LaneError):
            lane.fetch("https://www.truepeoplesearch.com/find/a/b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
