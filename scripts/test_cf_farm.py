#!/usr/bin/env python3
"""scripts/cf_farm.py 单测：全内存 mock，不起真浏览器、不出网，3 秒内完成。"""
import io
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cf_farm as farm

_REAL_THREAD = threading.Thread  # setUp 会 patch 掉 Thread，真线程用这个


class FakeResp:
    """模拟 scrapling response：只带 cookies / request_headers / body。"""

    def __init__(self, cookies=None, headers=None, body=b""):
        self.cookies = cookies
        self.request_headers = headers or {}
        self.body = body


class FakeSession:
    """模拟 StealthySession：记录 fetch 调用，不起浏览器。"""

    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.fetch_calls: list = []
        self.closed = False
        self.cookies_to_return = [{"name": "cf_clearance", "value": "abc123"}]
        self.ua_to_return = "Mozilla/5.0 FakeUA"
        self.body_to_return = b""
        self.raise_on_fetch: Exception | None = None
        FakeSession.instances.append(self)

    def start(self):
        return None

    def fetch(self, url, **kwargs):
        self.fetch_calls.append((url, kwargs))
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        return FakeResp(
            cookies=self.cookies_to_return,
            headers={"User-Agent": self.ua_to_return},
            body=self.body_to_return,
        )

    def close(self):
        self.closed = True


def make_handler(path="/healthz", body_bytes=b"", headers=None):
    """构造不依赖 socket 的 FarmHandler 实例，捕获输出。"""
    h = farm.FarmHandler.__new__(farm.FarmHandler)
    h.path = path
    h.headers = headers or {}
    h.rfile = io.BytesIO(body_bytes)
    h.wfile = io.BytesIO()
    h._out = {"code": None, "headers": {}}
    h.address_string = lambda: "127.0.0.1"  # type: ignore[method-assign]

    def send_response(code, message=None):
        h._out["code"] = code

    def send_header(k, v):
        h._out["headers"][k] = v

    def end_headers():
        pass

    h.send_response = send_response  # type: ignore[method-assign]
    h.send_header = send_header  # type: ignore[method-assign]
    h.end_headers = end_headers  # type: ignore[method-assign]
    return h


def read_json(handler):
    raw = handler.wfile.getvalue()
    # _send_json 只写 JSON；healthz 写 b"ok"
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw


class ParseRequestTests(unittest.TestCase):
    def test_ok_full(self):
        url, proxy, timeout = farm._parse_request({
            "cmd": "request.get",
            "url": "https://x.example/",
            "proxy": {"url": "http://p:8080"},
            "maxTimeout": 30000,
        })
        self.assertEqual(url, "https://x.example/")
        self.assertEqual(proxy, "http://p:8080")
        self.assertEqual(timeout, 30000)

    def test_cmd_missing(self):
        with self.assertRaises(ValueError):
            farm._parse_request({"url": "https://x/"})

    def test_cmd_wrong(self):
        with self.assertRaises(ValueError):
            farm._parse_request({"cmd": "sessions.create", "url": "https://x/"})

    def test_body_not_dict(self):
        for bad in (None, [], "str", 123):
            with self.assertRaises(ValueError):
                farm._parse_request(bad)

    def test_url_missing(self):
        with self.assertRaises(ValueError):
            farm._parse_request({"cmd": "request.get"})

    def test_url_blank_and_non_str(self):
        for bad in ("", "   ", None, 123, ["https://x/"]):
            with self.assertRaises(ValueError):
                farm._parse_request({"cmd": "request.get", "url": bad})

    def test_url_stripped(self):
        url, _, _ = farm._parse_request(
            {"cmd": "request.get", "url": "  https://x/a  "})
        self.assertEqual(url, "https://x/a")

    def test_proxy_missing_defaults_direct(self):
        _, proxy, _ = farm._parse_request(
            {"cmd": "request.get", "url": "https://x/"})
        self.assertEqual(proxy, "")

    def test_proxy_str_form(self):
        _, proxy, _ = farm._parse_request(
            {"cmd": "request.get", "url": "https://x/",
             "proxy": "http://p:8080"})
        self.assertEqual(proxy, "http://p:8080")

    def test_proxy_dict_empty(self):
        _, proxy, _ = farm._parse_request(
            {"cmd": "request.get", "url": "https://x/", "proxy": {}})
        self.assertEqual(proxy, "")

    def test_proxy_illegal_type_ignored(self):
        _, proxy, _ = farm._parse_request(
            {"cmd": "request.get", "url": "https://x/", "proxy": 12345})
        self.assertEqual(proxy, "")

    def test_max_timeout_missing_defaults(self):
        _, _, timeout = farm._parse_request(
            {"cmd": "request.get", "url": "https://x/"})
        self.assertEqual(timeout, farm.DEFAULT_MAX_TIMEOUT_MS)

    def test_max_timeout_illegal_defaults(self):
        for bad in ("not-a-number", "abc", [], {}):
            _, _, timeout = farm._parse_request(
                {"cmd": "request.get", "url": "https://x/",
                 "maxTimeout": bad})
            self.assertEqual(timeout, farm.DEFAULT_MAX_TIMEOUT_MS)

    def test_max_timeout_none_or_zero_defaults(self):
        # 实现用 `or DEFAULT`，None/0 都回落到默认值
        for bad in (None, 0):
            _, _, timeout = farm._parse_request(
                {"cmd": "request.get", "url": "https://x/",
                 "maxTimeout": bad})
            self.assertEqual(timeout, farm.DEFAULT_MAX_TIMEOUT_MS)

    def test_max_timeout_str_number_accepted(self):
        _, _, timeout = farm._parse_request(
            {"cmd": "request.get", "url": "https://x/",
             "maxTimeout": "25000"})
        self.assertEqual(timeout, 25000)


class SolutionMappingTests(unittest.TestCase):
    def test_extract_list_of_dict(self):
        resp = FakeResp(cookies=[
            {"name": "cf_clearance", "value": "abc"},
            {"name": "sess", "value": "1"},
        ])
        self.assertEqual(farm._extract_cookies(resp), [
            {"name": "cf_clearance", "value": "abc"},
            {"name": "sess", "value": "1"},
        ])

    def test_extract_dict_form(self):
        resp = FakeResp(cookies={"cf_clearance": "abc", "sess": "1"})
        out = farm._extract_cookies(resp)
        self.assertEqual({c["name"]: c["value"] for c in out},
                         {"cf_clearance": "abc", "sess": "1"})

    def test_extract_tuple_pairs(self):
        resp = FakeResp(cookies=[("cf_clearance", "abc"), ("sess", "1")])
        out = farm._extract_cookies(resp)
        self.assertEqual({c["name"]: c["value"] for c in out},
                         {"cf_clearance": "abc", "sess": "1"})

    def test_extract_skips_empty_name(self):
        resp = FakeResp(cookies=[{"name": "", "value": "x"},
                                 {"name": "ok", "value": "1"}])
        self.assertEqual(farm._extract_cookies(resp),
                         [{"name": "ok", "value": "1"}])

    def test_extract_none_is_empty(self):
        self.assertEqual(farm._extract_cookies(FakeResp(cookies=None)), [])

    def test_cookies_list_to_dict(self):
        # solution.cookies 列表转 dict（调用方 cf_solver 侧的映射语义）
        cookies = [{"name": "cf_clearance", "value": "abc123"},
                   {"name": "sess", "value": "1"}]
        d = {c["name"]: c["value"] for c in cookies}
        self.assertEqual(d.get("cf_clearance"), "abc123")

    def test_extract_ua_case_insensitive(self):
        resp = FakeResp(cookies=[{"name": "a", "value": "1"}],
                        headers={"user-agent": "UA-Lower"})
        self.assertEqual(farm._extract_ua(resp), "UA-Lower")
        resp2 = FakeResp(cookies=[{"name": "a", "value": "1"}],
                         headers={"User-Agent": "UA-Mixed"})
        self.assertEqual(farm._extract_ua(resp2), "UA-Mixed")

    def test_solve_once_empty_cookies_is_failure(self):
        with mock.patch.object(farm, "_new_session",
                               return_value=FakeSession()) as mk:
            sess = mk.return_value
            sess.cookies_to_return = []  # 无 cookie
            farm._reset_state()
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    farm.solve_once("https://x/", "", 10000)
                self.assertIn("no cookies", str(ctx.exception).lower())
            finally:
                farm._reset_state()

    def test_solve_once_clean_page_returns_html_without_cookies(self):
        with mock.patch.object(farm, "_new_session",
                               return_value=FakeSession()) as mk:
            sess = mk.return_value
            sess.cookies_to_return = []
            sess.body_to_return = "<html><body>" + "ok" * 100 + "</body></html>"
            farm._reset_state()
            try:
                cookies, _ua, html = farm.solve_once("https://x/", "", 10000)
                self.assertEqual(cookies, [])
                self.assertIn("ok", html)
            finally:
                farm._reset_state()

    def test_solve_once_success_returns_cookies_and_ua(self):
        with mock.patch.object(farm, "_new_session",
                               return_value=FakeSession()):
            farm._reset_state()
            try:
                cookies, ua, _html = farm.solve_once("https://x/", "", 10000)
                self.assertEqual(
                    {c["name"]: c["value"] for c in cookies}["cf_clearance"],
                    "abc123")
                self.assertIn("FakeUA", ua)
            finally:
                farm._reset_state()


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        farm._reset_state()
        FakeSession.instances.clear()

    def tearDown(self):
        farm._reset_state()

    def test_proxy_key(self):
        self.assertEqual(farm._proxy_key(""), "")
        self.assertEqual(farm._proxy_key("  "), "")
        self.assertEqual(farm._proxy_key("  http://p:8080  "),
                         "http://p:8080")

    def test_same_proxy_reuses_session(self):
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()) as mk:
            s1 = farm._get_session("http://p:8080")
            s2 = farm._get_session("http://p:8080")
            self.assertIs(s1, s2)
            self.assertEqual(mk.call_count, 1)

    def test_diff_proxy_isolated(self):
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()):
            sa = farm._get_session("http://a:8080")
            sb = farm._get_session("http://b:8080")
            self.assertIsNot(sa, sb)

    def test_threads_do_not_share_sessions(self):
        """不同工作线程同代理各持会话（浏览器对象绝不跨线程）。"""
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()) as mk:
            farm._reset_state()
            main_sess = farm._get_session("http://p:1")
            other: list = []
            th = _REAL_THREAD(
                target=lambda: other.append(farm._get_session("http://p:1")))
            th.start()
            th.join(timeout=10)
            self.assertEqual(len(other), 1)
            self.assertIsNot(other[0], main_sess)
            self.assertIs(farm._get_session("http://p:1"), main_sess)
            self.assertEqual(mk.call_count, 2)

    def test_direct_forms_share_one_entry(self):
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()) as mk:
            s1 = farm._get_session("")
            s2 = farm._get_session("   ")
            self.assertIs(s1, s2)
            self.assertEqual(mk.call_count, 1)
            self.assertIs(farm._get_entry(""), farm._get_entry("   "))

    def test_success_resets_fail_counter(self):
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()):
            farm._get_entry("http://p:1")
            farm._sessions[farm._proxy_key("http://p:1")]["fails"] = 2
            farm._record_success("http://p:1")
            self.assertEqual(
                farm._sessions[farm._proxy_key("http://p:1")]["fails"], 0)

    def test_rebuild_after_over_3_fails(self):
        """换代只 gen+1 不关会话；属主下次命中过期代时自己关旧会话并重建。"""
        created = []

        def _mk(proxy):
            s = FakeSession()
            created.append(s)
            return s

        with mock.patch.object(farm, "_new_session", side_effect=_mk):
            farm._reset_state()
            old = farm._get_session("http://p:1")
            gen0 = farm._sessions[farm._proxy_key("http://p:1")]["gen"]
            for _ in range(farm.MAX_FAILS_BEFORE_REBUILD):
                farm._record_failure("http://p:1")
            # 3 次内不重建：同代同会话
            self.assertIs(farm._get_session("http://p:1"), old)
            farm._record_failure("http://p:1")  # 第 4 次触发换代
            entry = farm._sessions[farm._proxy_key("http://p:1")]
            self.assertEqual(entry["gen"], gen0 + 1)
            self.assertEqual(entry["fails"], 0)
            self.assertFalse(old.closed)  # 换代不关任何会话
            new = farm._get_session("http://p:1")  # 懒建新会话，属主关旧会话
            self.assertIsNot(new, old)
            self.assertTrue(old.closed)

    def test_other_thread_session_survives_rebuild(self):
        """换代不关其他线程会话；属主线程下次触达时自己换新。"""
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()):
            farm._reset_state()
            ready, go, res = threading.Event(), threading.Event(), {}

            def worker():
                res["old"] = farm._get_session("http://p:1")
                ready.set()
                self.assertTrue(go.wait(timeout=10))
                res["new"] = farm._get_session("http://p:1")

            th = _REAL_THREAD(target=worker)
            th.start()
            self.assertTrue(ready.wait(timeout=10))
            for _ in range(farm.MAX_FAILS_BEFORE_REBUILD + 1):
                farm._record_failure("http://p:1")
            self.assertFalse(res["old"].closed)  # 其他线程会话安然无恙
            go.set()
            th.join(timeout=10)
            self.assertFalse(th.is_alive())
            self.assertIsNot(res["new"], res["old"])
            self.assertTrue(res["old"].closed)  # 属主自己关旧会话

    def test_fetch_exception_counts_failure(self):
        def _mk(proxy):
            s = FakeSession()
            s.raise_on_fetch = ConnectionError("down")
            return s

        with mock.patch.object(farm, "_new_session", side_effect=_mk):
            farm._reset_state()
            entry = farm._get_entry("http://p:1")
            with self.assertRaises(RuntimeError):
                farm.solve_once("https://x/", "http://p:1", 10000)
            self.assertEqual(entry["fails"], 1)

    def test_solve_once_reuses_session_across_calls(self):
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()) as mk:
            farm._reset_state()
            farm.solve_once("https://x/1", "http://p:1", 10000)
            farm.solve_once("https://x/2", "http://p:1", 10000)
            self.assertEqual(mk.call_count, 1)
            total_fetches = sum(len(s.fetch_calls)
                                for s in FakeSession.instances)
            self.assertEqual(total_fetches, 2)


class ColdStartSessionTests(unittest.TestCase):
    """R1 冷启动同代理会话增殖回归：4 并发只建 1 个浏览器会话。"""

    def setUp(self):
        farm._reset_state()
        FakeSession.instances.clear()
        self._saved_owner = dict(farm._lane_owner)

    def tearDown(self):
        farm._reset_state()
        farm._lane_owner.clear()
        farm._lane_owner.update(self._saved_owner)

    def test_cold_start_same_proxy_builds_single_session(self):
        proxy = "http://r1-cold:8080"
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()) as mk:
            farm._reset_state()
            errs: list = []
            bar = threading.Barrier(4)

            def job():
                try:
                    bar.wait(timeout=10)
                    farm.solve_once("https://example.com/", proxy, 10000)
                except Exception as exc:  # noqa: BLE001
                    errs.append(exc)

            ths = [_REAL_THREAD(target=job) for _ in range(4)]
            for th in ths:
                th.start()
            for th in ths:
                th.join(timeout=30)
            self.assertTrue(all(not t.is_alive() for t in ths))
            self.assertEqual(errs, [])
            self.assertEqual(mk.call_count, 1)


class LaneConcurrencyTests(unittest.TestCase):
    """并发 wedge 回归：fetch 串行但不持 entry 锁，建浏览器等待在锁外。"""

    def setUp(self):
        farm._reset_state()
        FakeSession.instances.clear()

    def tearDown(self):
        farm._reset_state()

    def test_same_proxy_fetches_serialize(self):
        peak = {"n": 0, "max": 0}
        lk = threading.Lock()

        def _mk(proxy):
            s = FakeSession()

            def fetch(url, **kw):
                with lk:
                    peak["n"] += 1
                    peak["max"] = max(peak["max"], peak["n"])
                time.sleep(0.2)
                try:
                    return FakeSession.fetch(s, url, **kw)
                finally:
                    with lk:
                        peak["n"] -= 1

            s.fetch = fetch
            return s

        with mock.patch.object(farm, "_new_session", side_effect=_mk):
            farm._reset_state()
            farm._get_session("http://p:1")  # 主线程先占一个亲和会话
            errs: list = []
            bar = threading.Barrier(2)

            def job():
                try:
                    bar.wait(timeout=10)
                    farm.solve_once("https://x/", "http://p:1", 10000)
                except Exception as exc:  # noqa: BLE001
                    errs.append(exc)

            ths = [_REAL_THREAD(target=job) for _ in range(2)]
            for th in ths:
                th.start()
            for th in ths:
                th.join(timeout=15)
            self.assertTrue(all(not t.is_alive() for t in ths))
            self.assertEqual(errs, [])
            self.assertEqual(peak["max"], 1)  # 同代理 fetch 真串行

    def test_fetch_does_not_hold_entry_lock(self):
        """慢 fetch 期间元数据锁可用：多 lane 互不阻塞。"""
        gate = threading.Event()

        def _mk(proxy):
            s = FakeSession()
            s.fetch = lambda url, **kw: (gate.wait(timeout=10), FakeSession.fetch(
                s, url, **kw))[1]
            return s

        with mock.patch.object(farm, "_new_session", side_effect=_mk):
            farm._reset_state()
            errs: list = []
            th = _REAL_THREAD(
                target=lambda: farm.solve_once("https://x/", "http://p:1",
                                               10000))
            try:
                th.start()
                time.sleep(0.3)  # 等工作线程进入 fetch
                t0 = time.monotonic()
                farm._record_success("http://p:1")  # 旧实现持锁 60s 会卡死
                entry = farm._get_entry("http://p:1")
                with entry["lock"]:
                    pass
                self.assertLess(time.monotonic() - t0, 5)
                self.assertEqual(errs, [])
            finally:
                gate.set()
                th.join(timeout=10)
            self.assertFalse(th.is_alive())

    def test_new_session_waits_outside_entry_lock(self):
        """建浏览器信号量排队时，元数据锁仍可用。"""
        farm._reset_state()
        farm._get_entry("http://p:1")
        sem = farm._new_session_sem
        drained = 0
        while sem.acquire(blocking=False):  # 占满全部建浏览器许可
            drained += 1
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()):
            try:
                done: list = []
                th = _REAL_THREAD(
                    target=lambda: done.append(
                        farm._get_session("http://p:1")))
                th.start()
                time.sleep(0.3)
                t0 = time.monotonic()
                farm._record_success("http://p:1")  # 旧实现会被建会话等待卡住
                with farm._get_entry("http://p:1")["lock"]:
                    pass
                self.assertLess(time.monotonic() - t0, 5)
                self.assertEqual(done, [])  # 工作线程仍在等信号量
                self.assertTrue(th.is_alive())
            finally:
                for _ in range(drained):
                    sem.release()
                th.join(timeout=10)
            self.assertEqual(len(done), 1)


class LoopIsolationTests(unittest.TestCase):
    """lane 线程 asyncio-loop 残留回归：同线程绝不双活 playwright。

    真实 Playwright 约束：同一线程第 1 个 sync_playwright 还活着时，
    第 2 个 start() 必报 "Sync API inside the asyncio loop"（已用真
    playwright 实测验证）；同线程 close 后重建则成功。所以 _get_session
    必须先关本线程其他会话再建新会话。用带单 loop 守卫的 mock 复现。
    """

    def setUp(self):
        farm._reset_state()
        FakeSession.instances.clear()

    def tearDown(self):
        farm._reset_state()

    def _guarded_new_session(self):
        """模拟 playwright 单线程单 loop：同线程双活即抛 loop 错。"""
        tls = threading.local()

        def _mk(proxy):
            live = getattr(tls, "live", None)
            if live is None:
                live = set()
                tls.live = live
            if live:
                raise RuntimeError(
                    "It looks like you are using Playwright Sync API "
                    "inside the asyncio loop.")
            s = FakeSession()
            live.add(s)

            orig_close = s.close

            def _close_and_untrack():
                orig_close()
                live.discard(s)

            s.close = _close_and_untrack  # type: ignore[method-assign]
            return s

        return _mk

    def test_same_thread_second_proxy_does_not_hit_loop(self):
        with mock.patch.object(farm, "_new_session",
                               side_effect=self._guarded_new_session()):
            sa = farm._get_session("http://a:8080")
            sb = farm._get_session("http://b:8080")  # 旧实现在此抛 loop 错
            self.assertIsNot(sa, sb)
            self.assertTrue(sa.closed)  # 先关后建，旧会话已关
            self.assertFalse(sb.closed)

    def test_same_thread_rebuild_after_gen_bump_does_not_hit_loop(self):
        with mock.patch.object(farm, "_new_session",
                               side_effect=self._guarded_new_session()):
            old = farm._get_session("http://p:1")
            for _ in range(farm.MAX_FAILS_BEFORE_REBUILD + 1):
                farm._record_failure("http://p:1")
            new = farm._get_session("http://p:1")  # 换代同线程重建
            self.assertIsNot(new, old)
            self.assertTrue(old.closed)

    def test_lane_routing_sticky_and_spread(self):
        """一致性路由：同代理永远同 lane；不同代理尽量散开（R6 稳定靠它）。"""
        saved = dict(farm._lane_owner)
        try:
            farm._lane_owner.clear()
            lanes = [farm._lane_for(f"http://p:{i}") for i in range(4)]
            self.assertEqual(len(set(lanes)), min(4, len(farm._LANES)))
            for i in range(4):
                self.assertEqual(farm._lane_for(f"http://p:{i}"), lanes[i])
        finally:
            farm._lane_owner.clear()
            farm._lane_owner.update(saved)


class EntryBoundsTests(unittest.TestCase):
    """表项 LRU 上限 + 闲置 TTL 回收。"""

    def setUp(self):
        farm._reset_state()
        FakeSession.instances.clear()
        self._saved_max = farm._MAX_ENTRIES
        self._saved_ttl = farm._IDLE_TTL_S

    def tearDown(self):
        farm._MAX_ENTRIES = self._saved_max
        farm._IDLE_TTL_S = self._saved_ttl
        farm._reset_state()

    def test_lru_cap_evicts_oldest(self):
        farm._MAX_ENTRIES = 3
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()):
            sess = {i: farm._get_session(f"http://p:{i}") for i in range(3)}
            victim = sess[1]
            farm._get_entry("http://p:0")  # 摸一下 p:0，p:1 变最久未用
            farm._get_entry("http://p:3")  # 触发淘汰
            keys = set(farm._sessions.keys())
            self.assertLessEqual(len(keys), 3)
            self.assertNotIn("http://p:1", keys)
            self.assertIn("http://p:0", keys)
            self.assertIn("http://p:3", keys)
            self.assertTrue(victim.closed)

    def test_idle_ttl_reaps(self):
        farm._IDLE_TTL_S = 100
        with mock.patch.object(farm, "_new_session",
                               side_effect=lambda p: FakeSession()):
            old = farm._get_session("http://old:1")
            entry = farm._sessions[farm._proxy_key("http://old:1")]
            with entry["lock"]:
                entry["last_used"] = time.monotonic() - 1000
            farm._get_entry("http://new:1")  #  miss 路径触发回收
            self.assertNotIn(farm._proxy_key("http://old:1"), farm._sessions)
            self.assertTrue(old.closed)

    def test_busy_entry_not_reaped(self):
        farm._IDLE_TTL_S = 100
        farm._get_entry("http://busy:1")
        entry = farm._sessions[farm._proxy_key("http://busy:1")]
        with entry["lock"]:
            entry["last_used"] = time.monotonic() - 1000
        self.assertTrue(entry["fetch_sem"].acquire(blocking=False))  # 模拟 fetch 中
        try:
            farm._get_entry("http://other:1")
            self.assertIn(farm._proxy_key("http://busy:1"), farm._sessions)
        finally:
            entry["fetch_sem"].release()


class FakeSock:
    """最小 socket 替身：只支持预读判断。"""

    def __init__(self, data: bytes):
        self._data = data
        self.timeout = None

    def gettimeout(self):
        return self.timeout

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n, flags=0):
        return self._data[:n]


class HealthzBypassTests(unittest.TestCase):
    def test_probe_detect(self):
        self.assertTrue(farm._is_healthz_probe(
            FakeSock(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")))
        self.assertTrue(farm._is_healthz_probe(
            FakeSock(b"GET /healthz?x=1 HTTP/1.1\r\n\r\n")))
        self.assertFalse(farm._is_healthz_probe(
            FakeSock(b"GET /other HTTP/1.1\r\n\r\n")))
        self.assertFalse(farm._is_healthz_probe(
            FakeSock(b"POST /v1 HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}")))
        self.assertFalse(farm._is_healthz_probe(FakeSock(b"")))

    def test_healthz_bypasses_pool(self):
        server = farm._PoolHTTPServer.__new__(farm._PoolHTTPServer)
        server._pool = mock.MagicMock()
        server._handle_request = mock.MagicMock()
        server.process_request(
            FakeSock(b"GET /healthz HTTP/1.1\r\n\r\n"), ("127.0.0.1", 1))
        server._handle_request.assert_called_once()
        server._pool.submit.assert_not_called()

    def test_v1_goes_to_pool(self):
        server = farm._PoolHTTPServer.__new__(farm._PoolHTTPServer)
        server._pool = mock.MagicMock()
        server._handle_request = mock.MagicMock()
        server.process_request(
            FakeSock(b"POST /v1 HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"),
            ("127.0.0.1", 1))
        server._pool.submit.assert_called_once()
        server._handle_request.assert_not_called()


class HealthzTests(unittest.TestCase):
    def test_healthz_ok(self):
        h = make_handler(path="/healthz")
        h.do_GET()
        self.assertEqual(h._out["code"], 200)
        self.assertEqual(h.wfile.getvalue(), b"ok")
        self.assertIn("text/plain", h._out["headers"].get("Content-Type", ""))

    def test_healthz_with_query_ok(self):
        h = make_handler(path="/healthz?x=1")
        h.do_GET()
        self.assertEqual(h._out["code"], 200)
        self.assertEqual(h.wfile.getvalue(), b"ok")

    def test_get_unknown_404(self):
        h = make_handler(path="/nope")
        h.do_GET()
        self.assertEqual(h._out["code"], 404)
        self.assertEqual(read_json(h)["status"], "error")

    def test_post_wrong_path_404(self):
        body = json.dumps({"cmd": "request.get",
                           "url": "https://x/"}).encode()
        h = make_handler(path="/v2", body_bytes=body,
                         headers={"Content-Length": str(len(body))})
        h.do_POST()
        self.assertEqual(h._out["code"], 404)

    def test_post_invalid_json_400(self):
        raw = b"{not-json"
        h = make_handler(path="/v1", body_bytes=raw,
                         headers={"Content-Length": str(len(raw))})
        h.do_POST()
        self.assertEqual(h._out["code"], 400)
        self.assertEqual(read_json(h)["status"], "error")

    def test_post_bad_cmd_400(self):
        body = json.dumps({"cmd": "nope", "url": "https://x/"}).encode()
        h = make_handler(path="/v1", body_bytes=body,
                         headers={"Content-Length": str(len(body))})
        h.do_POST()
        self.assertEqual(h._out["code"], 400)
        self.assertEqual(read_json(h)["status"], "error")

    def test_post_solve_error_returns_status_error(self):
        body = json.dumps({"cmd": "request.get",
                           "url": "https://x/"}).encode()
        h = make_handler(path="/v1", body_bytes=body,
                         headers={"Content-Length": str(len(body))})
        with mock.patch.object(farm, "solve_once",
                               side_effect=RuntimeError("boom")):
            h.do_POST()
        obj = read_json(h)
        self.assertEqual(obj["status"], "error")
        self.assertIn("boom", obj["message"])

    def test_post_success_shape(self):
        body = json.dumps({"cmd": "request.get", "url": "https://x/",
                           "maxTimeout": 20000}).encode()
        h = make_handler(path="/v1", body_bytes=body,
                         headers={"Content-Length": str(len(body))})
        cookies = [{"name": "cf_clearance", "value": "abc123"}]
        with mock.patch.object(farm, "solve_once",
                               return_value=(cookies, "FakeUA", "<html>hi</html>")):
            h.do_POST()
        obj = read_json(h)
        self.assertEqual(obj["status"], "ok")
        self.assertEqual(obj["solution"]["cookies"], cookies)
        self.assertEqual(obj["solution"]["userAgent"], "FakeUA")
        self.assertEqual(obj["solution"]["response"], "<html>hi</html>")


class ProxyHeaderTests(unittest.TestCase):
    def test_full_headers_build_full_proxy_url(self):
        h = {"X-Proxy-Server": "http://gw:8080",
             "X-Proxy-Username": "u1", "X-Proxy-Password": "p1"}
        self.assertEqual(farm._proxy_from_headers(h), "http://u1:p1@gw:8080")

    def test_no_headers_empty(self):
        self.assertEqual(farm._proxy_from_headers({}), "")
        self.assertEqual(farm._proxy_from_headers(None), "")

    def test_post_uses_header_proxy_when_body_has_none(self):
        body = json.dumps({"cmd": "request.get", "url": "https://x/",
                           "maxTimeout": 20000}).encode()
        h = make_handler(path="/v1", body_bytes=body,
                         headers={"Content-Length": str(len(body)),
                                  "X-Proxy-Server": "http://gw:8080",
                                  "X-Proxy-Username": "u1",
                                  "X-Proxy-Password": "p1"})
        seen = {}

        def fake_solve(url, proxy_url="", timeout_ms=60000):
            seen["proxy"] = proxy_url
            return ([{"name": "cf_clearance", "value": "abc"}], "UA", "")

        with mock.patch.object(farm, "solve_once", side_effect=fake_solve):
            h.do_POST()
        self.assertEqual(seen["proxy"], "http://u1:p1@gw:8080")
        self.assertEqual(read_json(h)["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
