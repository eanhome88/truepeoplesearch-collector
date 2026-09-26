#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from discover import is_blocked_html
from scrape_to_tidb import is_captcha_document

CAPTCHA_URL = (
    "https://www.truepeoplesearch.com/InternalCaptcha"
    "?returnUrl=https%3A%2F%2Fwww.truepeoplesearch.com%2Ffind%2Fperson%2Fabc"
)
PERSON_URL = "https://www.truepeoplesearch.com/find/person/abc"
NORMAL_HTML = """<html>
<head><title>Jane Doe, Age 42</title></head>
<body>
Jane Doe
Age 42
Lives in Denver, CO
</body>
</html>
"""


class TestCaptchaGuard(unittest.TestCase):
    def test_internal_captcha_url_with_empty_html(self):
        self.assertTrue(is_captcha_document(CAPTCHA_URL, ""))

    def test_just_a_moment_html(self):
        html = (
            "<html><head><title>Just a moment...</title></head>"
            "<body>Just a moment</body></html>"
        )
        self.assertTrue(is_captcha_document(PERSON_URL, html))

    def test_normal_name_page_is_not_captcha(self):
        self.assertFalse(is_captcha_document(PERSON_URL, NORMAL_HTML))

    def test_blocked_html_internal_captcha_url(self):
        self.assertTrue(
            is_blocked_html("<html><body>ok</body></html>", url=CAPTCHA_URL)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
