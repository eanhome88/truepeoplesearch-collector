#!/usr/bin/env python3
"""
协议层抓取与代理池模块自动化测试
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from proxy_pool import ProxyManager, ProxyNode
from protocol_fetcher import check_cloudflare_blocked, CloudflareChallengeError, EmptyPageError
from scrapling.parser import Adaptor
from scrape_to_tidb import parse_person


class TestProxyManager(unittest.TestCase):
    def test_tunnel_proxy(self):
        pm = ProxyManager(tunnel="username:secret@127.0.0.1:8888")
        self.assertEqual(pm.tunnel, "http://username:secret@127.0.0.1:8888")
        masked = pm._mask_proxy(pm.tunnel)
        self.assertNotIn("secret", masked)
        self.assertIn("****", masked)

    def test_proxy_file_and_cooldown(self):
        # 临时创建一个代理文件
        test_file = Path(_SCRIPT_DIR) / "_temp_proxies.txt"
        test_file.write_text("http://p1:8080\nhttp://p2:8080\n", encoding="utf-8")

        try:
            pm = ProxyManager(proxy_file=str(test_file), cooldown_sec=10.0)
            self.assertEqual(pm.total_count, 2)

            async def _check():
                p1 = await pm.get_proxy()
                self.assertIsNotNone(p1)
                # 报告 CF 拦截，应进入冷却
                await pm.report_result(p1, success=False, is_cf_block=True)
                p2 = await pm.get_proxy()
                self.assertIsNotNone(p2)
                self.assertNotEqual(p1, p2)

            asyncio.run(_check())
        finally:
            if test_file.exists():
                test_file.unlink()


class TestProtocolParsing(unittest.TestCase):
    def test_cf_detection(self):
        # 403 / 503
        self.assertTrue(check_cloudflare_blocked(403, "Forbidden"))
        self.assertTrue(check_cloudflare_blocked(503, "Service Unavailable"))

        # 200 包含 challenge
        cf_html = "<html><head><title>Just a moment...</title></head><body>Please wait while your request is being verified</body></html>"
        self.assertTrue(check_cloudflare_blocked(200, cf_html))

        # 正常 HTML 不被拦截
        normal_html = "<html><head><title>Jamie Perez, Age 45</title></head><body>Age 45 Lives in Thornton, CO</body></html>"
        self.assertFalse(check_cloudflare_blocked(200, normal_html))

    def test_html_parsing_fidelity(self):
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Jamie Perez, Age 45, Thornton, CO | TruePeopleSearch.com</title></head>
        <body>
            <div class="card-header">Jamie Perez</div>
            <div class="content">
                Age 45
                Born December 1980
                Lives in Thornton, CO
                does not appear to be married
                Current Address
                This is the most recently reported
                address
                9595 Pecos St #704
                Thornton, CO 80260
                $79,000 | 1 Bath | 1,056 Sq Ft | Built 1993
                Adams County
                Phone Numbers
                (303) 210-9670 Wireless Possible Primary Last reported August 2026 AT&T
                Email Addresses
                jay.yake@gmail.com
                Current Address Property Details
            </div>
        </body>
        </html>
        """
        doc = Adaptor(sample_html)
        url = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"
        data = parse_person(doc, url)

        self.assertEqual(data["person_id"], "px82l44nur68u2l8n60")
        self.assertEqual(data["full_name"], "Jamie Perez")
        self.assertEqual(data["age"], 45)
        self.assertEqual(data["birth_month"], 12)
        self.assertEqual(data["birth_year"], 1980)
        self.assertEqual(data["current_city"], "Thornton")
        self.assertEqual(data["current_state"], "CO")
        self.assertEqual(data["marital_status"], "single")

        # 当前地址
        addr = data["current_address"]
        self.assertEqual(addr.get("street"), "9595 Pecos St #704")
        self.assertEqual(addr.get("city"), "Thornton")
        self.assertEqual(addr.get("state"), "CO")
        self.assertEqual(addr.get("zip_code"), "80260")
        self.assertEqual(addr.get("estimated_value"), 79000.0)

        # 电话
        self.assertTrue(len(data["phone_numbers"]) >= 1)
        phone = data["phone_numbers"][0]
        self.assertEqual(phone["phone_number"], "(303) 210-9670")

        # 邮箱
        self.assertTrue(len(data["emails"]) >= 1)
        self.assertEqual(data["emails"][0]["email"], "jay.yake@gmail.com")

    def test_parse_person_lean(self):
        from protocol_fetcher import parse_person_lean
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Jamie Perez, Age 45, Thornton, CO | TruePeopleSearch.com</title></head>
        <body>
            <div class="card-header">Jamie Perez</div>
            <div class="content">
                Age 45
                Born December 1980
                Lives in Thornton, CO
                Current Address
                9595 Pecos St #704
                Thornton, CO 80260
                $79,000 | 1 Bath | 1,056 Sq Ft | Built 1993
                Adams County
                Phone Numbers
                (303) 210-9670 Wireless Possible Primary Last reported August 2026 AT&T
                Email Addresses
                jay.yake@gmail.com
                Previous Addresses
            </div>
        </body>
        </html>
        """
        doc = Adaptor(sample_html)
        url = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"
        data = parse_person_lean(doc, url)

        self.assertEqual(data["full_name"], "Jamie Perez")
        self.assertEqual(data["age"], 45)
        self.assertEqual(data["current_city"], "Thornton")
        self.assertEqual(data["current_state"], "CO")
        self.assertEqual(data["current_address"]["street"], "9595 Pecos St #704")
        self.assertTrue(len(data["phone_numbers"]) >= 1)
        self.assertTrue(len(data["emails"]) >= 1)
        self.assertEqual(data["aliases"], [])
        self.assertEqual(data["previous_addresses"], [])


if __name__ == "__main__":
    unittest.main()
