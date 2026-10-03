"""穿云 v2 Cookie 模式的离线契约测试，不访问穿云或目标站。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cloudbypass_v2 as cb


class _Headers(dict):
    def get_list(self, key):
        value = self.get(key)
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]


class _Response:
    def __init__(self, status, headers, text):
        self.status_code = status
        self.headers = _Headers(headers)
        self.text = text


class _SyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        return self.responses.pop(0)

    def close(self):
        return None


class V2ContractTest(unittest.TestCase):
    def test_failed_request_costs_nothing_and_challenge_is_two_extra(self):
        self.assertEqual(cb.credit_points(False, True), 0)
        self.assertEqual(cb.credit_points(True, False), 1)
        self.assertEqual(cb.credit_points(True, True), 3)

    def test_warm_session_drops_the_challenge_fee(self):
        book = cb.SessionBook()
        book._free = [7]
        part = book.checkout()
        first, challenged = book.take(part, True, now=100.0)
        second, again = book.take(part, True, now=160.0)
        failed, _ = book.take(part, False, now=200.0)
        later, fresh = book.take(part, True, now=160.0 + cb.SESSION_SEC + 1)
        self.assertEqual((first, challenged), (3, True))
        self.assertEqual((second, again), (1, False))
        self.assertEqual(failed, 0)
        self.assertEqual((later, fresh), (3, True))

    def test_plain_success_skips_challenge_fee(self):
        self.assertFalse(cb.saw_challenge({"x-cb-status": "ok"}))
        self.assertTrue(cb.saw_challenge({"x-cb-challenge": "solved"}))
        headers = cb.build_headers(
            "www.truepeoplesearch.com",
            "k",
            "http://acct-res_US:secret@gw-res.cloudbypass.com:1288",
            "https",
            "",
            60,
        )
        self.assertNotIn("x-cb-part", headers)
        self.assertNotIn("_s", headers["x-cb-proxy"])
    def test_headers_match_console_cookie_mode(self):
        headers = cb.build_headers(
            "www.truepeoplesearch.com",
            "test-key",
            "user:pass@gw-res.cloudbypass.com:1288",
            "https",
            "",
            60,
        )
        self.assertEqual(headers["x-cb-apikey"], "test-key")
        self.assertEqual(headers["x-cb-host"], "www.truepeoplesearch.com")
        self.assertEqual(headers["x-cb-version"], "2")
        self.assertEqual(headers["x-cb-proxy"], "user:pass@gw-res.cloudbypass.com:1288")
        self.assertEqual(headers["x-cb-options"], "full-cookie")
        self.assertNotIn("x-cb-part", headers)
        self.assertNotIn("x-cb-fp", headers)
        self.assertNotIn("x-cb-sitekey", headers)
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("force", headers["x-cb-options"])

    def test_proxy_without_scheme_gets_http(self):
        self.assertEqual(
            cb.normalize_proxy("user:pass@gw-res.cloudbypass.com:1288"),
            "http://user:pass@gw-res.cloudbypass.com:1288",
        )

    def test_cookie_from_first_response_is_sent_on_retry(self):
        html = "<html><body>" + ("phone " * 20) + "</body></html>"
        client = _SyncClient([
            _Response(403, {"set-cookie": "cb_clearance=encrypted; Path=/", "x-cb-status": "fail"}, "challenge"),
            _Response(200, {"x-cb-status": "ok"}, html),
        ])
        page = cb.fetch_sync(
            "https://www.truepeoplesearch.com/find/person/abc?x=1",
            apikey="test-key",
            proxy="http://user:pass@proxy:1288",
            max_retries=2,
            client=client,
            cookies=cb.CookieStore(),
        )
        self.assertIsNotNone(page)
        self.assertEqual(page.status, 200)
        self.assertEqual(page.cb_status, "ok")
        self.assertEqual(
            client.calls[0][0],
            "https://api.cloudbypass.com/find/person/abc?x=1",
        )
        self.assertNotIn("Cookie", client.calls[0][1])
        self.assertEqual(client.calls[1][1]["Cookie"], "cb_clearance=encrypted")
        self.assertNotIn("x-cb-part", client.calls[1][1])

    def test_missing_credentials_returns_none(self):
        self.assertIsNone(
            cb.fetch_sync(
                "https://www.truepeoplesearch.com/find/person/abc",
                apikey="",
                proxy="",
            )
        )

    def test_rate_limit_raises(self):
        client = _SyncClient([
            _Response(429, {"x-cb-status": "fail"}, '{"error":"TOO_MANY_REQUESTS"}'),
        ])
        with self.assertRaises(cb.GatewayError) as caught:
            cb.fetch_sync(
                "https://www.truepeoplesearch.com/find/person/abc",
                apikey="test-key",
                proxy="http://user:pass@proxy:1288",
                max_retries=1,
                client=client,
                cookies=cb.CookieStore(),
            )
        self.assertEqual(caught.exception.kind, "rate_limit")


if __name__ == "__main__":
    unittest.main()
