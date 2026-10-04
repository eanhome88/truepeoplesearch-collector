#!/usr/bin/env python3
"""电话链路优化回归：查询号兜底 / VoIP-无类型回收 / 全文兜底 / 错误分类 / 开关。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("TPS_ACCEPT_VOIP", "1")
os.environ.setdefault("TPS_ACCEPT_UNKNOWN_TYPE", "1")
os.environ.setdefault("TPS_REQUIRE_PHONE", "1")

import scrape_to_tidb as s


class FakePage:
    def __init__(self, html, url="", status=200):
        self.html_content = html
        self.url = url
        self.status = status
        self.response = None


class PhoneOptTests(unittest.TestCase):
    def test_digits_from_phone_url(self):
        self.assertEqual(s._phone_digits_from_url("https://www.truepeoplesearch.com/find/phone/2015550123"), "2015550123")
        self.assertEqual(s._phone_digits_from_url("https://x/results?resultphone=12015550123"), "2015550123")
        self.assertEqual(s._phone_digits_from_url("https://www.truepeoplesearch.com/find/person/abc"), "")

    def test_synthesize_queried_phone_valid(self):
        synth = s._synthesize_queried_phone("https://www.truepeoplesearch.com/find/phone/2015550123")
        self.assertIsNotNone(synth)
        self.assertEqual(synth["phone_number"], "(201) 555-0123")
        self.assertTrue(s._valid_us_phone(synth["phone_number"]))

    def test_synthesize_queried_phone_invalid_sequential(self):
        # 格式非法（如 111 开头）不合成，不污染库；feeder 顺序号本身格式合法，
        # 空结果走 EmptyPageError 分支不会进合成路径，所以这里只断言非法号。
        self.assertIsNone(s._synthesize_queried_phone("https://www.truepeoplesearch.com/find/phone/1111111111"))
        # 顺序号格式合法时合成成功（仅在重定向到人物页分支使用）
        synth = s._synthesize_queried_phone("https://www.truepeoplesearch.com/find/phone/2012000001")
        self.assertIsNotNone(synth)
        self.assertEqual(synth["phone_number"], "(201) 200-0001")

    def test_voip_accepted_by_default(self):
        data = {"phone_numbers": [{"phone_number": "(201) 555-0123", "line_type": "Voip"}]}
        self.assertTrue(s.has_usable_phone(data))

    def test_unknown_type_accepted_by_default(self):
        data = {"phone_numbers": [{"phone_number": "(201) 555-0123", "line_type": None}]}
        self.assertTrue(s.has_usable_phone(data))

    def test_fallback_scan_without_heading(self):
        text = "John Smith\n(201) 555-0123 - Verizon\nLast reported Aug 2026\n"
        phones = s.extract_phone_numbers(text)
        self.assertTrue(phones)
        self.assertEqual(phones[0]["phone_number"], "(201) 555-0123")

    def test_fallback_scan_empty_section(self):
        text = "Phone Numbers\n\nEmail Addresses\nfoo@bar.com\nCall (201) 555-0123 for info\n"
        phones = s.extract_phone_numbers(text)
        self.assertTrue(phones)

    def test_raise_fetch_error_empty_response_is_retry_not_empty_page(self):
        with self.assertRaises(s.ScrapeError) as ctx:
            s._raise_fetch_error(RuntimeError("net::ERR_EMPTY_RESPONSE for http://x"), "http://x")
        self.assertEqual(ctx.exception.bucket, "retry")
        self.assertNotIn("empty page", str(ctx.exception).lower())

    def test_raise_fetch_error_timeout_stays_cf_fail(self):
        with self.assertRaises(s.FetchTimeoutError) as ctx:
            s._raise_fetch_error(RuntimeError("net::ERR_TIMED_OUT"), "http://x")
        self.assertEqual(ctx.exception.bucket, "cf_fail")

    def test_ingest_search_result_carries_queried_phone(self):
        html = '<a href="/find/person/px82l44nur68u2l8n60">a</a><a href="/find/person/qq99">b</a>'
        page = FakePage(html, "https://www.truepeoplesearch.com/find/phone/2015550123")
        import unittest.mock as mock
        fake_redis = mock.MagicMock()
        with mock.patch.object(s, "_queue_redis", return_value=fake_redis), \
             mock.patch.dict("sys.modules", {"tps_queue": mock.MagicMock(feed=mock.MagicMock(return_value={"fed": 2})),
                                             "phone_plan": mock.MagicMock(note_phone_lookup=mock.MagicMock(),
                                                                           remember_associated_phones=mock.MagicMock(return_value=1))}):
            out = s.ingest_response(page, "https://www.truepeoplesearch.com/find/phone/2015550123", mock.MagicMock())
        self.assertTrue(out.get("is_search_result"))
        self.assertEqual(out.get("count"), 2)
        self.assertEqual(out.get("queried_phone"), "(201) 555-0123")

    def test_require_phone_switch(self):
        import importlib
        os.environ["TPS_REQUIRE_PHONE"] = "0"
        importlib.reload(s)
        try:
            self.assertFalse(s.REQUIRE_PHONE)
            # 无电话人物在开关关掉后可入库判定通过（insert 的门）
            self.assertFalse(s.has_usable_phone({"phone_numbers": []}))
        finally:
            os.environ["TPS_REQUIRE_PHONE"] = "1"
            importlib.reload(s)
        self.assertTrue(s.REQUIRE_PHONE)


class ClassifyTests(unittest.TestCase):
    def test_worker_classify_net_errors_retry(self):
        import distributed_worker as dw
        for msg in ("net::ERR_EMPTY_RESPONSE", "ERR_CONNECTION_CLOSED boom", "read: connection reset by peer"):
            self.assertEqual(dw._classify_error_kind(RuntimeError(msg)), "retry", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
