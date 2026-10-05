#!/usr/bin/env python3
"""TPS_OWN_CF=1: solver contract, adapters, and the worker's protocol fetch path."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cf_solver  # noqa: E402
from cf_solver import (  # noqa: E402
    CfSolution,
    CfSolver,
    CfSolverError,
    impersonate_for_ua,
    normalize_solution,
    require_cf_solver,
    sid_for_proxy,
)

UA_131 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

CALLS: list = []


def fake_solve_sync(url, proxy=None, user_agent=None, timeout=45):
    CALLS.append({"url": url, "proxy": proxy, "user_agent": user_agent})
    return {"cookies": {"cf_clearance": f"tok{len(CALLS)}"}, "user_agent": user_agent or UA_131}


async def fake_solve_async(url, proxy=None, user_agent=None, timeout=45):
    CALLS.append({"url": url, "proxy": proxy, "user_agent": user_agent})
    return {"data": {"cookie": [{"name": "cf_clearance", "value": "async"}], "ua": UA_131}}


def fake_solve_fail(url, proxy=None, user_agent=None, timeout=45):
    CALLS.append({"url": url})
    return {"ok": False, "error": "turnstile interactive"}


class TestNormalize(unittest.TestCase):
    def test_dict_cookies_and_ua_variants(self):
        for key in ("user_agent", "ua", "userAgent"):
            sol = normalize_solution({"cookies": {"cf_clearance": "a", "__cf_bm": "b"}, key: UA_131})
            self.assertEqual(sol.cookies, {"cf_clearance": "a", "__cf_bm": "b"})
            self.assertEqual(sol.user_agent, UA_131)
            self.assertIsNone(sol.html)
            self.assertEqual(sol.cookie_header(), "cf_clearance=a; __cf_bm=b")

    def test_list_and_string_cookies(self):
        sol = normalize_solution({"cookies": [{"name": "cf_clearance", "value": "x"}, "k=v"]})
        self.assertEqual(sol.cookies, {"cf_clearance": "x", "k": "v"})
        sol = normalize_solution('{"cookies": "cf_clearance=y; __cf_bm=z", "ttl": 600}')
        self.assertEqual(sol.cookies, {"cf_clearance": "y", "__cf_bm": "z"})
        self.assertEqual(sol.ttl, 600.0)

    def test_wrapped_and_html_only(self):
        sol = normalize_solution({"result": {"html": "<html>person</html>"}})
        self.assertEqual(sol.cookies, {})
        self.assertEqual(sol.html, "<html>person</html>")
        self.assertFalse(sol.has_clearance)

    def test_failures(self):
        with self.assertRaises(CfSolverError):
            normalize_solution({"ok": False, "error": "nope"})
        with self.assertRaises(CfSolverError):
            normalize_solution({"user_agent": UA_131})
        with self.assertRaises(CfSolverError):
            normalize_solution("not json")
        with self.assertRaises(CfSolverError):
            normalize_solution([1, 2])


class TestImpersonate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("TPS_IMPERSONATE", None)

    def test_exact_and_nearest_lower(self):
        self.assertEqual(impersonate_for_ua(UA_131), "chrome131")
        self.assertEqual(impersonate_for_ua(UA_131.replace("131", "125")), "chrome124")
        self.assertEqual(impersonate_for_ua(UA_131.replace("131", "50")), "chrome99")

    def test_default_and_override(self):
        self.assertEqual(impersonate_for_ua("Mozilla/5.0 Firefox/120"), "chrome124")
        self.assertEqual(impersonate_for_ua(None), "chrome124")
        os.environ["TPS_IMPERSONATE"] = "chrome136"
        try:
            self.assertEqual(impersonate_for_ua(UA_131), "chrome136")
        finally:
            os.environ.pop("TPS_IMPERSONATE", None)

    def test_sid_for_proxy(self):
        self.assertEqual(sid_for_proxy(""), "")
        self.assertEqual(sid_for_proxy("http://u-region-us-sid-abc12345-t-30:p@gw:1"), "abc12345")
        self.assertEqual(sid_for_proxy("http://u-session_zz99:p@gw:1"), "zz99")
        a = sid_for_proxy("http://u:p@10.0.0.1:8080")
        self.assertEqual(len(a), 12)
        self.assertEqual(a, sid_for_proxy("http://u:p@10.0.0.1:8080"))


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        body = json.dumps({
            "cookies": {"cf_clearance": "http-" + (payload.get("user_agent") or "first")[:5]},
            "user_agent": payload.get("user_agent") or UA_131,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestAdapters(unittest.TestCase):
    def setUp(self):
        CALLS.clear()

    def test_python_sync_and_async(self):
        solver = CfSolver(f"{__name__}:fake_solve_sync")
        sol = asyncio.run(solver.solve("https://x/1", proxy="http://u:p@h:1", user_agent=None))
        self.assertEqual(sol.cookies, {"cf_clearance": "tok1"})
        self.assertEqual(sol.user_agent, UA_131)
        self.assertEqual(CALLS[-1], {"url": "https://x/1", "proxy": "http://u:p@h:1", "user_agent": None})
        self.assertTrue(solver.describe().startswith("python "))

        solver = CfSolver(f"{__name__}:fake_solve_async")
        sol = asyncio.run(solver.solve("https://x/2"))
        self.assertEqual(sol.cookies, {"cf_clearance": "async"})

    def test_python_reports_failure(self):
        solver = CfSolver(f"{__name__}:fake_solve_fail")
        with self.assertRaises(CfSolverError) as ctx:
            asyncio.run(solver.solve("https://x/3"))
        self.assertIn("turnstile", str(ctx.exception))

    def test_cmd_adapter(self):
        script = Path(tempfile.mkdtemp()) / "solver.py"
        script.write_text(
            "import json,sys\n"
            "req=json.load(sys.stdin)\n"
            "print(json.dumps({'cookies':{'cf_clearance':'cmd-'+req['url'][-1]},'user_agent':req.get('user_agent') or 'UA Chrome/124.0'}))\n",
            encoding="utf-8",
        )
        solver = CfSolver(f'cmd:"{sys.executable}" "{script}"')
        sol = asyncio.run(solver.solve("https://x/7", proxy=None))
        self.assertEqual(sol.cookies, {"cf_clearance": "cmd-7"})
        self.assertIn("Chrome/124", sol.user_agent)

    def test_http_adapter(self):
        try:
            server = HTTPServer(("127.0.0.1", 0), _Handler)
        except (PermissionError, OSError) as exc:
            self.skipTest(f"Local socket bind not permitted: {exc}")
            return
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            solver = CfSolver(f"http://127.0.0.1:{server.server_port}/solve", timeout=10)
            try:
                sol = asyncio.run(solver.solve("https://x/9", user_agent=None))
            except CfSolverError as exc:
                if "Operation not permitted" in str(exc):
                    self.skipTest(f"Local socket connect not permitted: {exc}")
                    return
                raise
            self.assertEqual(sol.cookies, {"cf_clearance": "http-first"})
            self.assertEqual(sol.user_agent, UA_131)
            self.assertTrue(solver.describe().startswith("http 127.0.0.1:"))
        finally:
            server.shutdown()
            server.server_close()

    def test_bad_specs_and_env(self):
        for spec in ("", "nonsense", "no.such.module:fn", f"{__name__}:UA_131"):
            with self.assertRaises(CfSolverError):
                CfSolver(spec)
        os.environ.pop("TPS_CF_SOLVER", None)
        self.assertIsNone(cf_solver.load_cf_solver_from_env())
        with self.assertRaises(CfSolverError) as ctx:
            require_cf_solver()
        self.assertIn("TPS_OWN_CF=1", str(ctx.exception))
        os.environ["TPS_CF_SOLVER"] = f"{__name__}:fake_solve_sync"
        try:
            self.assertEqual(require_cf_solver().kind, "python")
        finally:
            os.environ.pop("TPS_CF_SOLVER", None)


class _DummyClient:
    def __init__(self):
        self.cookies = {}
        self.closed = False

    async def close(self):
        self.closed = True


class TestWorkerOwnCfPath(unittest.TestCase):
    """Drive _fetch_page_own_cf with a scripted transport; no network, no browser."""

    def setUp(self):
        CALLS.clear()
        import distributed_worker as dw

        self.dw = dw
        self._saved = (dw.OWN_CF_BYPASS, dw._OWN_CF_SOLVER, dw.publish_warmed, dw._OwnCfSession._new_client, dw._OwnCfSession.get)
        dw.OWN_CF_BYPASS = True
        dw._OWN_CF_SOLVER = CfSolver(f"{__name__}:fake_solve_sync")
        dw.publish_warmed = lambda *a, **k: None
        dw._OwnCfSession._new_client = lambda self, impersonate: _DummyClient()
        self.responses: list = []
        self.gets: list = []
        tests = self

        async def scripted_get(session, url):
            html = session.pending_html.pop(url, None)
            if html is not None:
                return 200, html
            tests.gets.append(url)
            return tests.responses.pop(0)

        dw._OwnCfSession.get = scripted_get

    def tearDown(self):
        dw = self.dw
        dw.OWN_CF_BYPASS, dw._OWN_CF_SOLVER, dw.publish_warmed, dw._OwnCfSession._new_client, dw._OwnCfSession.get = self._saved

    def _box(self):
        return self.dw._ChromeBox(2, proxy="http://u:p@gw:1288")

    def _run(self, box, url="https://www.truepeoplesearch.com/find/person/p1"):
        return asyncio.run(self.dw._fetch_page(box, url))

    def test_first_page_solves_then_fetches(self):
        box = self._box()
        self.responses = [(200, "<html><body>" + "person data " * 10 + "</body></html>")]
        page = self._run(box)
        self.assertEqual(page.status, 200)
        self.assertTrue(box.warm)
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(CALLS[0]["proxy"], "http://u:p@gw:1288")
        self.assertIsNone(CALLS[0]["user_agent"])
        self.assertEqual(box.session.impersonate, "chrome131")
        self.assertEqual(box.session.cookies, {"cf_clearance": "tok1"})

        # Second page on the same box reuses the clearance; no new solve.
        self.responses = [(200, "<html>" + "more " * 20 + "</html>")]
        self._run(box, "https://www.truepeoplesearch.com/find/person/p2")
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(len(self.gets), 2)

    def test_site_captcha_does_not_resolve(self):
        box = self._box()
        self.responses = [(200, "<html>InternalCaptcha please verify</html>")]
        with self.assertRaises(self.dw.HttpError) as ctx:
            self._run(box)
        self.assertEqual(getattr(ctx.exception, "cf_route", ""), "rotate_proxy")
        self.assertEqual(len(CALLS), 1)

    def test_challenge_triggers_one_resolve_with_same_ua(self):
        box = self._box()
        self.responses = [
            (403, "<html><title>Just a moment...</title></html>"),
            (200, "<html>" + "ok " * 30 + "</html>"),
        ]
        page = self._run(box)
        self.assertEqual(page.status, 200)
        self.assertEqual(len(CALLS), 2)
        self.assertEqual(CALLS[1]["user_agent"], UA_131)
        self.assertEqual(box.session.cookies, {"cf_clearance": "tok2"})

    def test_persistent_challenge_is_captcha_rate_limit(self):
        box = self._box()
        self.responses = [
            (503, "<html>cf-turnstile</html>"),
            (503, "<html>cf-turnstile</html>"),
        ]
        with self.assertRaises(self.dw.HttpError) as ctx:
            self._run(box)
        self.assertEqual(self.dw.classify_error(ctx.exception), "rate_limit")
        self.assertIn("captcha", str(ctx.exception).lower())
        self.assertEqual(len(CALLS), 2)

    def test_429_and_404_pass_through(self):
        box = self._box()
        self.responses = [(429, "")]
        with self.assertRaises(self.dw.HttpError) as ctx:
            self._run(box)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(self.dw.classify_error(ctx.exception), "rate_limit")
        self.assertEqual(len(CALLS), 1)

        self.responses = [(404, "<html>not found</html>")]
        page = self._run(box, "https://www.truepeoplesearch.com/find/person/gone")
        self.assertEqual(page.status, 404)

    def test_solver_failure_on_first_clearance(self):
        self.dw._OWN_CF_SOLVER = CfSolver(f"{__name__}:fake_solve_fail")
        box = self._box()
        with self.assertRaises(self.dw.HttpError) as ctx:
            self._run(box)
        self.assertEqual(self.dw.classify_error(ctx.exception), "rate_limit")
        self.assertFalse(box.warm)
        self.assertEqual(self.gets, [])

    def test_solver_html_is_used_directly(self):
        def solve_with_html(url, proxy=None, user_agent=None, timeout=45):
            CALLS.append({"url": url})
            return {"cookies": {"cf_clearance": "h"}, "user_agent": UA_131, "html": "<html>" + "inline " * 20 + "</html>"}

        self.dw._OWN_CF_SOLVER = CfSolver(f"{__name__}:fake_solve_sync")
        self.dw._OWN_CF_SOLVER._target = solve_with_html
        box = self._box()
        page = self._run(box)
        self.assertEqual(page.status, 200)
        self.assertEqual(self.gets, [])
        self.assertIn("inline", page.get_all_text())

    def test_session_recycle_resolves(self):
        box = self._box()
        self.responses = [(200, "<html>" + "a " * 30 + "</html>")]
        self._run(box)
        box.served = self.dw.SESSION_RECYCLE_PAGES
        box.warm = False
        self.responses = [(200, "<html>" + "b " * 30 + "</html>")]
        self._run(box, "https://www.truepeoplesearch.com/find/person/p3")
        self.assertEqual(len(CALLS), 2)
        self.assertEqual(box.served, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
