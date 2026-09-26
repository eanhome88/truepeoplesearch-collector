#!/usr/bin/env python3
import unittest

from tps_scale import (
    COLD_PAGE_SEC,
    DAILY_TARGET,
    MAX_BROWSER_CONCURRENCY,
    WARM_PAGE_SEC,
    browsers_for,
    chrome_process_count,
    daily_capacity,
    host_browser_budget,
    resolve_browsers,
)


class TestSessionPlan(unittest.TestCase):
    def test_warm_session_kwargs_do_not_launch_a_browser(self):
        from scrapling.engines._browsers._stealth import StealthySession
        from scrape_to_tidb import fetch_kwargs, session_kwargs

        kwargs = session_kwargs()
        self.assertTrue(kwargs["disable_resources"])
        self.assertTrue(kwargs["block_ads"])
        self.assertTrue(fetch_kwargs()["disable_resources"])
        self.assertNotIn("block_ads", fetch_kwargs())
        self.assertTrue(kwargs["solve_cloudflare"])
        self.assertEqual(kwargs["retries"], 1)
        self.assertEqual(kwargs["max_pages"], 1)
        self.assertNotIn("headless", fetch_kwargs())
        session = StealthySession(**kwargs)
        self.assertFalse(session._is_alive)


class TestProtocolPage(unittest.TestCase):
    def test_document_keeps_name_and_age_and_rejects_challenge(self):
        from scrape_to_tidb import html_page, is_challenge_html, parse_person

        self.assertTrue(is_challenge_html("<title>Just a moment...</title>"))
        self.assertTrue(is_challenge_html(""))
        html = (
            "<html><head><title>Jane Doe, Age 42</title></head>"
            "<body>Age 42 Lives in Denver, CO</body></html>"
        )
        self.assertFalse(is_challenge_html(html))
        url = "https://www.truepeoplesearch.com/find/person/abc123"
        page = html_page(html, 200, url)
        data = parse_person(page, url)
        self.assertEqual(data["full_name"], "Jane Doe")
        self.assertEqual(data["age"], 42)
        self.assertEqual(data["current_city"], "Denver")
        self.assertEqual(data["current_state"], "CO")
        self.assertEqual(page.status, 200)


class TestTpsScale(unittest.TestCase):
    def test_three_million_warm_vs_cold(self):
        warm = browsers_for(DAILY_TARGET, WARM_PAGE_SEC)
        cold = browsers_for(DAILY_TARGET, COLD_PAGE_SEC)
        self.assertEqual(warm, 278)
        self.assertEqual(cold, 1042)
        self.assertGreaterEqual(daily_capacity(warm, WARM_PAGE_SEC), DAILY_TARGET)
        self.assertLess(daily_capacity(warm - 1, WARM_PAGE_SEC), DAILY_TARGET)

    def test_host_budget_leaves_memory_for_the_os(self):
        self.assertEqual(host_browser_budget(mem_gb=16, cpu_count=16), 20)
        self.assertEqual(host_browser_budget(mem_gb=8, cpu_count=16), 6)
        self.assertEqual(host_browser_budget(mem_gb=4, cpu_count=16), 1)
        self.assertEqual(host_browser_budget(mem_gb=128, cpu_count=16), 48)
        self.assertLessEqual(host_browser_budget(mem_gb=512, cpu_count=64), MAX_BROWSER_CONCURRENCY)

    def test_resolve_auto_does_not_request_cold_thread_count(self):
        plan = resolve_browsers(None, DAILY_TARGET, WARM_PAGE_SEC, mem_gb=16, cpu_count=16)
        self.assertEqual(plan["browsers"], 20)
        self.assertEqual(plan["need"], 278)
        self.assertEqual(plan["hosts"], 14)
        self.assertEqual(plan["cold_browsers"], 1042)
        self.assertLess(plan["browsers"], plan["cold_browsers"])

    def test_explicit_concurrency_is_clamped(self):
        plan = resolve_browsers(500, DAILY_TARGET, WARM_PAGE_SEC, mem_gb=64, cpu_count=16)
        self.assertEqual(plan["browsers"], MAX_BROWSER_CONCURRENCY)
        self.assertTrue(plan["clamped"])
        self.assertEqual(resolve_browsers(4, mem_gb=16, cpu_count=16)["browsers"], 4)

    def test_tabs_share_one_chrome(self):
        self.assertEqual(chrome_process_count(16, 4), 4)
        self.assertEqual(chrome_process_count(1, 4), 1)
        self.assertEqual(chrome_process_count(4, 4), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
