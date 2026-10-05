#!/usr/bin/env python3
"""scripts/cf_solver.py 里 _solve_flaresolverr 单测：只测本函数，不起浏览器、不出网。

用手写假 urlopen（按脚本内调用记录请求），覆盖：
- 代理 userinfo 拆分 / 同站判定；
- byparr 风味：代理走 X-Proxy-* 请求头、maxTimeout/max_timeout 双发、body 无代理；
- flaresolverr 风味：无认证代理走无状态单发（兼容自建 cf_farm）；
  带认证代理走 sessions.create 建会话复用；建会话失败自动降级无状态；
- 成功必须带 cf_clearance、落点必须同站、response 带回 html；
- maxTimeout 钳制 1s~120s，socket 超时 = maxTimeout + 10s。
"""
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cf_solver import (
    CfSolver,
    CfSolverError,
    _looks_like_dead_session,
    _same_site,
    _split_proxy_auth,
)

TARGET = "https://www.truepeoplesearch.com/find/person/x"
SOL_URL = "https://www.truepeoplesearch.com/find/person/x?y=1"


def _ok_doc(url=SOL_URL, cookies=None, ua="UA-1", html="<html>hi</html>"):
    if cookies is None:
        cookies = [{"name": "cf_clearance", "value": "abc"}, {"name": "__cf_bm", "value": "b"}]
    return {"status": "ok", "solution": {"cookies": cookies, "userAgent": ua,
                                         "url": url, "response": html,
                                         "status": 200, "headers": {}}}


class FakeHTTPResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTransport:
    """假 urlopen：按脚本编排响应，记下每次请求的 body/headers/timeout。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append({"url": req.full_url,
                           "body": json.loads(req.data.decode("utf-8")),
                           "headers": dict(req.headers),
                           "timeout": timeout})
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return FakeHTTPResp(json.dumps(action).encode("utf-8"))


def _http_error(code, message):
    body = json.dumps({"status": "error", "message": message}).encode("utf-8")
    return urllib.error.HTTPError("http://x/v1", code, "ERR", {}, io.BytesIO(body))


class FlareTest(unittest.TestCase):
    def setUp(self):
        self._real = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self._real

    def _install(self, script):
        transport = FakeTransport(script)
        urllib.request.urlopen = transport
        return transport

    # ---- 纯函数 ----

    def test_split_proxy_auth(self):
        bare, user, pwd = _split_proxy_auth("http://u1:p%40ss@gw:8080")
        self.assertEqual(bare, "http://gw:8080")
        self.assertEqual(user, "u1")
        self.assertEqual(pwd, "p@ss")

    def test_split_proxy_no_auth(self):
        self.assertEqual(_split_proxy_auth("http://gw:8080"),
                         ("http://gw:8080", "", ""))
        self.assertEqual(_split_proxy_auth(""), ("", "", ""))

    def test_same_site(self):
        self.assertTrue(_same_site(TARGET, SOL_URL))
        self.assertTrue(_same_site("https://truepeoplesearch.com/a",
                                   "https://www.truepeoplesearch.com/b"))
        self.assertFalse(_same_site(TARGET, "https://evil.example.com/z"))
        self.assertTrue(_same_site(TARGET, ""))

    def test_dead_session_hint(self):
        self.assertTrue(_looks_like_dead_session("session invalid, create a new one"))
        self.assertFalse(_looks_like_dead_session("Could not bypass challenge"))

    # ---- byparr 风味 ----

    def test_byparr_proxy_goes_to_headers_not_body(self):
        t = self._install([_ok_doc()])
        solver = CfSolver("byparr:http://127.0.0.1:8191/v1")
        self.assertEqual(solver._flavor, "byparr")
        out = solver._solve_flaresolverr(
            {"url": TARGET, "proxy": "http://u1:p1@gw:8080", "timeout": 45}, 45)
        self.assertEqual(out["cookies"]["cf_clearance"], "abc")
        self.assertEqual(out["html"], "<html>hi</html>")
        call = t.calls[0]
        self.assertNotIn("proxy", call["body"])
        self.assertEqual(call["body"]["maxTimeout"], 45000)
        self.assertEqual(call["body"]["max_timeout"], 45)
        headers = {k.lower(): v for k, v in call["headers"].items()}
        self.assertEqual(headers.get("x-proxy-server"), "http://gw:8080")
        self.assertEqual(headers.get("x-proxy-username"), "u1")
        self.assertEqual(headers.get("x-proxy-password"), "p1")

    def test_byparr_no_proxy_no_proxy_headers(self):
        t = self._install([_ok_doc()])
        solver = CfSolver("byparr:http://127.0.0.1:8191/v1")
        solver._solve_flaresolverr({"url": TARGET, "timeout": 45}, 45)
        headers = {k.lower() for k in t.calls[0]["headers"]}
        self.assertFalse({h for h in headers if h.startswith("x-proxy-")})

    # ---- flaresolverr 无认证：无状态单发 ----

    def test_flare_plain_proxy_stateless(self):
        t = self._install([_ok_doc()])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        out = solver._solve_flaresolverr(
            {"url": TARGET, "proxy": "http://gw:8080", "timeout": 45}, 45)
        self.assertEqual(out["user_agent"], "UA-1")
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.calls[0]["body"]["proxy"], {"url": "http://gw:8080"})
        self.assertNotIn("session", t.calls[0]["body"])

    # ---- flaresolverr 认证代理：建会话复用 ----

    def test_flare_auth_proxy_creates_and_reuses_session(self):
        t = self._install([{"status": "ok"}, _ok_doc(), _ok_doc()])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        payload = {"url": TARGET, "proxy": "http://u1:p1@gw:8080", "timeout": 45}
        solver._solve_flaresolverr(payload, 45)
        solver._solve_flaresolverr(payload, 45)
        self.assertEqual(len(t.calls), 3)  # 1 建会话 + 2 次复用
        create = t.calls[0]["body"]
        self.assertEqual(create["cmd"], "sessions.create")
        self.assertEqual(create["proxy"],
                         {"url": "http://gw:8080", "username": "u1", "password": "p1"})
        sid = create["session"]
        self.assertTrue(sid)
        self.assertEqual(t.calls[1]["body"]["session"], sid)
        self.assertEqual(t.calls[1]["body"]["proxy"], {"url": "http://gw:8080"})
        self.assertEqual(t.calls[2]["body"]["session"], sid)

    def test_flare_session_create_unsupported_falls_back_stateless(self):
        # 自建 cf_farm 不支持 sessions.create：400 后降级，且之后不再试探。
        t = self._install([_http_error(400, "unsupported cmd 'sessions.create'"),
                           _ok_doc(), _ok_doc()])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        payload = {"url": TARGET, "proxy": "http://u1:p1@gw:8080", "timeout": 45}
        solver._solve_flaresolverr(payload, 45)
        solver._solve_flaresolverr(payload, 45)
        self.assertFalse(solver._flare_session_ok)
        bodies = [c["body"] for c in t.calls]
        self.assertEqual(bodies[1]["cmd"], "request.get")
        self.assertNotIn("session", bodies[1])
        self.assertNotIn("session", bodies[2])
        self.assertEqual(len(t.calls), 3)  # 第二次直接无状态，不再建会话

    def test_stateless_fallback_keeps_full_proxy_with_auth(self):
        # 无状态回退必须带全量代理（含认证），否则 cf_farm 侧认证丢失。
        t = self._install([_http_error(400, "unsupported cmd 'sessions.create'"),
                           _ok_doc()])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        solver._solve_flaresolverr(
            {"url": TARGET, "proxy": "http://u1:p1@gw:8080", "timeout": 45}, 45)
        body = t.calls[1]["body"]
        self.assertEqual(body["proxy"], {"url": "http://u1:p1@gw:8080"})

    # ---- 成功判定 ----

    def test_missing_clearance_is_error(self):
        t = self._install([_ok_doc(cookies=[{"name": "NID", "value": "x"}])])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        with self.assertRaises(CfSolverError) as ctx:
            solver._solve_flaresolverr({"url": TARGET, "timeout": 45}, 45)
        self.assertIn("cf_clearance", str(ctx.exception))
        self.assertIn("NID", str(ctx.exception))

    def test_offsite_landing_is_error(self):
        t = self._install([_ok_doc(url="https://evil.example.com/z")])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        with self.assertRaises(CfSolverError) as ctx:
            solver._solve_flaresolverr({"url": TARGET, "timeout": 45}, 45)
        self.assertIn("off-site", str(ctx.exception))

    def test_clean_html_without_clearance_is_accepted(self):
        clean = "<html><head><title>John Smith</title></head><body>" + "x" * 200 + "</body></html>"
        t = self._install([_ok_doc(cookies=[{"name": "_ga", "value": "g"}],
                                   html=clean)])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        out = solver._solve_flaresolverr({"url": TARGET, "timeout": 45}, 45)
        self.assertEqual(out["html"], clean)

    def test_challenge_html_without_clearance_is_rejected(self):
        bad = "<html><head><title>Just a moment</title></head><body>" + "y" * 200 + "</body></html>"
        t = self._install([_ok_doc(cookies=[{"name": "_ga", "value": "g"}],
                                   html=bad)])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        with self.assertRaises(CfSolverError) as ctx:
            solver._solve_flaresolverr({"url": TARGET, "timeout": 45}, 45)
        self.assertIn("cf_clearance", str(ctx.exception))

    def test_error_status_surfaces_message(self):
        t = self._install([{"status": "error", "message": "Challenge not detected!"}])
        solver = CfSolver("byparr:http://127.0.0.1:8191/v1")
        with self.assertRaises(CfSolverError) as ctx:
            solver._solve_flaresolverr({"url": TARGET, "timeout": 45}, 45)
        self.assertIn("Challenge not detected", str(ctx.exception))

    def test_http_error_json_message_parsed(self):
        t = self._install([_http_error(500, "Could not bypass challenge")])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        with self.assertRaises(CfSolverError) as ctx:
            solver._solve_flaresolverr({"url": TARGET, "timeout": 45}, 45)
        self.assertIn("Could not bypass challenge", str(ctx.exception))

    # ---- 超时钳制 ----

    def test_max_timeout_clamped_and_socket_aligned(self):
        t = self._install([_ok_doc()])
        solver = CfSolver("flaresolverr:http://127.0.0.1:8191/v1")
        solver._solve_flaresolverr({"url": TARGET, "timeout": 300}, 300)
        self.assertEqual(t.calls[0]["body"]["maxTimeout"], 120000)
        self.assertEqual(t.calls[0]["timeout"], 130.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
