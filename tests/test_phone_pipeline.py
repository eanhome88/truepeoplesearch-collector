# -*- coding: utf-8 -*-
"""
TruePeopleSearch 手机号反查、电话全量解析与客户业务规则完整单元测试
验证：
1. 11个电话全量捕获 (0丢漏、支持任意运营商、正确分类 Wireless/Landline/Voip)
2. 客户核心规则：主号若为座机，自动选取最近时间的无线手机号作为【当前电话】
3. 姓名分词 (名/中间名/姓) 与居住时长 (Jan 2012 - Aug 2026) 提取
4. 电话反查结果列表页 (resultphone) 自动发现人物线索并注入 Redis，严防生成脏 URL 档案
"""

import unittest
from unittest.mock import MagicMock, patch
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.scrape_to_tidb import (
    extract_person_id,
    split_full_name,
    extract_phone_numbers,
    parse_person,
    ingest_response,
    EmptyPageError,
)
from scripts.protocol_fetcher import parse_person_lean


class MockHtmlPage:
    def __init__(self, title: str, text: str, url: str = ""):
        self._title = title
        self._text = text
        self.url = url
        self.status = 200

    def get_all_text(self):
        return self._text

    def css(self, selector):
        class Node:
            def __init__(self, val):
                self.val = val
            def get(self):
                return self.val
        if "title" in selector:
            return Node(self._title)
        return Node("")


class TestPhonePipeline(unittest.TestCase):

    def test_extract_person_id(self):
        # 1. 正常人物 URL
        self.assertEqual(
            extract_person_id("https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"),
            "px82l44nur68u2l8n60"
        )
        self.assertEqual(
            extract_person_id("/find/person/px12345"),
            "px12345"
        )
        # 2. 电话反查 URL：决不能当作 person_id 返回！
        self.assertEqual(
            extract_person_id("https://www.truepeoplesearch.com/resultphone?phoneno=2012000000"),
            ""
        )
        self.assertEqual(extract_person_id(""), "")

    def test_split_full_name(self):
        cases = [
            ("Guadalupe Soliz", ("Guadalupe", "", "Soliz")),
            ("A Rene Jones", ("A", "Rene", "Jones")),
            ("Angeles De Leon Leon", ("Angeles", "De Leon", "Leon")),
            ("Mary I", ("Mary", "", "I")),
            ("Alicia Renee Thompson", ("Alicia", "Renee", "Thompson")),
            ("SingleToken", ("SingleToken", "", "")),
        ]
        for name, expected in cases:
            fn, mn, ln = split_full_name(name)
            self.assertEqual((fn, mn, ln), expected, f"Failed on name: {name}")

    def test_jamie_perez_all_11_phones_extraction(self):
        """测试 11 个电话号码 100% 完整提取（对标真实数据 jamie_perez_profile.md）"""
        sample_text = """
        Jamie Perez, Age 45, Thornton, CO | TruePeopleSearch.com
        Age 45
        Born December 1980
        Lives in Thornton, CO
        Current Address
        9595 Pecos St #704
        Thornton, CO 80260
        (Oct 2006 - Sep 2026)
        Adams County

        Phone Numbers
        (303) 210-9670 - Wireless - Possible Primary
        Last reported Aug 2026
        AT&T Mobility
        (303) 822-8055 - Landline
        Last reported Oct 2023
        Bijou Telephone Co.
        (303) 901-7106 - Wireless
        Last reported Mar 2011
        T-Mobile
        (720) 951-2581 - Wireless
        Last reported May 2020
        Verizon Wireless
        (303) 822-8009 - Landline
        Last reported Aug 2018
        Bijou Telephone Co.
        (303) 210-0967 - Wireless
        Last reported May 2020
        T-Mobile
        (303) 693-0688 - Landline
        Last reported Jul 2018
        Qwest
        (410) 744-8650 - Landline
        Last reported May 2020
        Verizon Maryland
        (303) 680-6493 - Landline
        Last reported Jul 2018
        Qwest
        (303) 766-9619 - Landline
        Last reported Aug 2009
        Qwest
        (303) 695-9148 - Landline
        Last reported Nov 2008
        Qwest

        Email Addresses
        jay.yake@gmail.com
        """
        page = MockHtmlPage("Jamie Perez, Age 45, Thornton, CO | TruePeopleSearch.com", sample_text)
        url = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"

        data = parse_person(page, url)

        # 1. 人物基础字段
        self.assertEqual(data["person_id"], "px82l44nur68u2l8n60")
        self.assertEqual(data["full_name"], "Jamie Perez")
        self.assertEqual(data["first_name"], "Jamie")
        self.assertEqual(data["last_name"], "Perez")
        self.assertEqual(data["age"], 45)
        self.assertEqual(data["address_duration"], "(Oct 2006 - Sep 2026)")

        # 2. 电话号码无遗漏
        self.assertEqual(len(data["phone_numbers"]), 11, "必须抓全全部 11 个电话号码")

        # 3. 主号优先无线
        self.assertEqual(data["primary_phone"], "(303) 210-9670")
        self.assertEqual(data["primary_phone_type"], "Wireless")

        # 4. 移动号码按时间排序
        self.assertEqual(data["wireless_phone_1"], "(303) 210-9670") # 2026.08
        self.assertIn(data["wireless_phone_2"], ["(720) 951-2581", "(303) 210-0967"]) # 2020.05
        self.assertIsNotNone(data["wireless_phone_3"])

        # 5. 电话列表包含所有号码
        for p in data["phone_numbers"]:
            self.assertIn(p["phone_number"], data["all_phones"])

    def test_customer_rule_primary_landline_fallback_to_recent_wireless(self):
        """
        测试客户截图提出的硬性规则：
        如果主要电话号码是座机，就找下面最近时间的无线
        """
        sample_text = """
        Jeffrey Edwards, Age 58, Broken Arrow, OK | TruePeopleSearch.com
        Current Address
        7313 S 241st East Ave
        Broken Arrow, OK 74014
        (Jul 2013 - Sep 2026)

        Phone Numbers
        (918) 663-4925 - Landline - Possible Primary
        Last reported Dec 2011
        AT&T
        (918) 275-4664 - Landline
        Last reported Feb 2020
        Totah Communications
        (919) 271-7436 - Wireless
        Last reported Aug 2021
        AT&T
        (918) 704-3577 - Wireless
        Last reported Aug 2026
        AT&T
        (918) 246-0458 - Landline
        Last reported May 2016
        AT&T

        Email Addresses
        edwards@example.com
        """
        page = MockHtmlPage("Jeffrey Edwards, Age 58, Broken Arrow, OK", sample_text)
        url = "https://www.truepeoplesearch.com/find/person/px4l68926lr49u4l820"

        data = parse_person(page, url)

        # 验证：主要电话虽标注为 (918) 663-4925 (座机)，但系统必须智能选取最近时间的无线 (918) 704-3577 (2026年8月)
        self.assertEqual(data["primary_phone"], "(918) 704-3577")
        self.assertEqual(data["primary_phone_type"], "Wireless")
        self.assertEqual(data["wireless_phone_1"], "(918) 704-3577")
        self.assertEqual(data["wireless_phone_2"], "(919) 271-7436")

    def test_search_results_page_enqueues_and_does_not_insert_fake_person(self):
        """测试按手机号反查时的搜索结果页，能发现人物并灌入队列，不生成伪人物"""
        search_html = """
        <!DOCTYPE html>
        <html>
        <head><title>(201) 200-0001 - TruePeopleSearch</title></head>
        <body>
            <div class="card">
                <a href="/find/person/px82l44nur68u2l8n60" class="btn">View All Details</a>
            </div>
            <div class="card">
                <a href="/find/person/px4l68926lr49u4l820" class="btn">View All Details</a>
            </div>
        </body>
        </html>
        """
        page = MockHtmlPage("(201) 200-0001 - TruePeopleSearch", search_html)
        page.html = search_html
        url = "https://www.truepeoplesearch.com/resultphone?phoneno=2012000001"

        mock_db = MagicMock()

        with patch("redis.Redis") as mock_redis_cls:
            mock_r = MagicMock()
            mock_redis_cls.return_value = mock_r
            with patch("tps_queue.feed") as mock_feed:
                mock_feed.return_value = {"enqueued": 2, "deduped": 0, "invalid": 0}

                result = ingest_response(page, url, mock_db)

                # 验证：成功识别为搜索页，并触发两名人物线索的注入
                self.assertTrue(result.get("is_search_result"))
                self.assertEqual(result.get("count"), 2)
                self.assertIn("px82l44nur68u2l8n60", result.get("person_ids"))
                self.assertIn("px4l68926lr49u4l820", result.get("person_ids"))

                # 验证：绝对不能向数据库插入伪人物记录
                self.assertEqual(mock_db.cursor.call_count, 0)


if __name__ == "__main__":
    unittest.main()
