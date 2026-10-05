#!/usr/bin/env python3
"""parse_person 404 空页回归：标题/正文命中 404 信号即返回空结果，不起浏览器、不出网。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrape_to_tidb as s

URL = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"


class _FakeCss:
    def __init__(self, title):
        self._title = title

    def get(self):
        return self._title


class FakePage:
    """手写假页面：只实现 parse_person 用到的 css/get_all_text/url/status。"""

    def __init__(self, title, text, status=200):
        self._title = title
        self._text = text
        self.url = URL
        self.status = status
        self.response = None

    def css(self, _expr):
        return _FakeCss(self._title)

    def get_all_text(self):
        return self._text


class ParsePerson404Tests(unittest.TestCase):
    def test_404_title_returns_empty(self):
        page = FakePage("404 Not Found", "404 Not Found\nSorry, page not found\n")
        data = s.parse_person(page, URL)
        self.assertIsNone(data["full_name"])
        self.assertIsNone(data["first_name"])
        self.assertEqual(data["phone_numbers"], [])

    def test_body_not_found_returns_empty_and_normal_page_keeps_name(self):
        gone = FakePage("Some Person - TruePeopleSearch",
                        "Oops, the page you are looking for was not found\n")
        self.assertIsNone(s.parse_person(gone, URL)["full_name"])
        # 404 区号电话正文不能误杀正常人物页（正文裸 404 不算 404 页信号）
        ok = FakePage("John Smith, Age 42",
                      "John Smith\nLives in Atlanta, GA\n(404) 555-0103 - Wireless\n")
        data = s.parse_person(ok, URL)
        self.assertEqual(data["full_name"], "John Smith")

    def test_dead_shell_size_returns_empty_but_marked_page_keeps_name(self):
        shell = FakePage("Gone - TruePeopleSearch", "x")
        shell.html = "x" * 68_000
        self.assertIsNone(s.parse_person(shell, URL)["full_name"])
        alive = FakePage("Jane Doe, Age 40", "Jane Doe")
        marker = "Lives in Austin. Current Address 1 Main. "
        alive.html = marker + ("." * (68_000 - len(marker)))
        data = s.parse_person(alive, URL)
        self.assertEqual(data["full_name"], "Jane Doe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
