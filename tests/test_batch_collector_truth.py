"""The sample collector must not claim DB success without a committed row."""

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "tools" / "batch_collector_100.py"
SPEC = importlib.util.spec_from_file_location("batch_collector_100_truth", PATH)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


class BatchCollectorTruthTests(unittest.TestCase):
    def test_rate_limit_stops_without_db_connection(self):
        page = mock.Mock(status=429, url="https://example.invalid/ratelimited")
        fetcher = mock.Mock()
        fetcher.fetch.return_value = page
        with mock.patch.object(collector, "get_db") as get_db:
            with self.assertRaises(collector.RateLimitedError):
                collector.scrape_single_person("https://example.invalid/person", None, fetcher)
        get_db.assert_not_called()
        self.assertEqual(fetcher.fetch.call_count, 1)

    def test_http_200_challenge_stops_without_parsing_or_db(self):
        page = mock.Mock(status=200, url="https://www.truepeoplesearch.com/find/person/p1")
        page.html_content = "<html><title>Just a moment</title></html>"
        fetcher = mock.Mock()
        fetcher.fetch.return_value = page
        with mock.patch.object(collector, "parse_person") as parse_person, \
             mock.patch.object(collector, "get_db") as get_db:
            with self.assertRaises(collector.RateLimitedError):
                collector.scrape_single_person("https://www.truepeoplesearch.com/find/person/p1", None, fetcher)
        parse_person.assert_not_called()
        get_db.assert_not_called()

    def test_no_phone_is_not_reported_as_committed(self):
        fetcher = mock.Mock()
        fetcher.fetch.return_value = mock.Mock(status=200, url="https://www.truepeoplesearch.com/find/person/p1")
        data = {"person_id": "sample-1", "full_name": "Sample", "phone_numbers": []}
        with mock.patch.object(collector, "parse_person", return_value=data):
            with mock.patch.object(collector, "get_db") as get_db:
                outcome, _ = collector.scrape_single_person("https://www.truepeoplesearch.com/find/person/p1", None, fetcher)
        self.assertEqual(outcome, "no_phone")
        get_db.assert_not_called()

    def test_new_row_requires_commit_and_post_write_read(self):
        fetcher = mock.Mock()
        fetcher.fetch.return_value = mock.Mock(status=200, url="https://www.truepeoplesearch.com/find/person/p2")
        data = {"person_id": "sample-2", "full_name": "Sample", "primary_phone": "(212) 555-0199", "primary_phone_type": "Wireless"}
        with mock.patch.object(collector, "parse_person", return_value=data), \
             mock.patch.object(collector, "get_db") as get_db, \
             mock.patch.object(collector, "_person_exists", side_effect=[False, True]), \
             mock.patch.object(collector, "insert_person", return_value=True):
            outcome, _ = collector.scrape_single_person("https://www.truepeoplesearch.com/find/person/p2", None, fetcher)
        self.assertEqual(outcome, "new")
        get_db.return_value.close.assert_called_once()

    def test_committed_false_is_not_reported_as_new(self):
        fetcher = mock.Mock()
        fetcher.fetch.return_value = mock.Mock(status=200, url="https://www.truepeoplesearch.com/find/person/p3")
        data = {"person_id": "sample-3", "full_name": "Sample", "primary_phone": "(212) 555-0199", "primary_phone_type": "Landline"}
        with mock.patch.object(collector, "parse_person", return_value=data), \
             mock.patch.object(collector, "get_db"), \
             mock.patch.object(collector, "_person_exists", return_value=False), \
             mock.patch.object(collector, "insert_person", return_value=False):
            outcome, _ = collector.scrape_single_person("https://www.truepeoplesearch.com/find/person/p3", None, fetcher)
        self.assertEqual(outcome, "not_committed")

    def test_discovery_rate_limit_returns_distinct_status(self):
        output = io.StringIO()
        with mock.patch.object(collector, "_configured_proxy", return_value=None), \
             mock.patch.object(collector, "StealthyFetcher"), \
             mock.patch.object(collector, "harvest_fresh_urls", side_effect=collector.RateLimitedError("HTTP 429")), \
             redirect_stdout(output):
            exit_code = collector.main(["3"])
        self.assertEqual(exit_code, 2)
        self.assertIn('"status": "rate_limited"', output.getvalue())
        self.assertNotIn("TPS_BATCH_PROGRESS", output.getvalue())

    def test_no_verified_records_finishes_partial_not_success(self):
        output = io.StringIO()
        with mock.patch.object(collector, "_configured_proxy", return_value=None), \
             mock.patch.object(collector, "StealthyFetcher"), \
             mock.patch.object(collector, "harvest_fresh_urls", return_value=["https://example.invalid/person"]), \
             mock.patch.object(collector, "scrape_single_person", return_value=("no_phone", "sample-4")), \
             redirect_stdout(output):
            exit_code = collector.main(["2"])
        self.assertEqual(exit_code, 3)
        self.assertIn('"status": "partial"', output.getvalue())
        self.assertIn('"new_rows": 0', output.getvalue())

    def test_saved_proxy_config_takes_precedence_over_old_process_env(self):
        with mock.patch.dict("os.environ", {"PROXY_TUNNEL": "http://old.example.invalid:9000"}), \
             mock.patch.object(collector.redis, "Redis"), \
             mock.patch.object(collector, "load_proxy_config", return_value={"mode": "direct"}):
            self.assertIsNone(collector._configured_proxy())

    def test_discovery_ignores_cross_origin_and_non_https_person_links(self):
        page = mock.Mock(status=200, url="https://www.truepeoplesearch.com/results")
        page.html_content = "<html></html>"
        page.get_all_text.return_value = "results"
        page.css.return_value.get.return_value = None
        page.css.return_value.getall.return_value = [
            "https://other.example.invalid/find/person/pbad",
            "//other.example.invalid/find/person/pbad2",
            "http://www.truepeoplesearch.com/find/person/pbad3",
            "https://www.truepeoplesearch.com.evil.invalid/find/person/pbad4",
            "/find/person/p123?tracking=1",
        ]
        fetcher = mock.Mock()
        fetcher.fetch.return_value = page
        self.assertEqual(
            collector.harvest_fresh_urls(fetcher, None, target_count=1),
            ["https://www.truepeoplesearch.com/find/person/p123"],
        )

    def test_offsite_redirect_stops_before_parsing_or_database_access(self):
        page = mock.Mock(status=200, url="https://other.example.invalid/find/person/p123")
        fetcher = mock.Mock()
        fetcher.fetch.return_value = page
        with mock.patch.object(collector, "parse_person") as parse_person, \
             mock.patch.object(collector, "get_db") as get_db:
            with self.assertRaises(collector.RateLimitedError):
                collector.scrape_single_person(
                    "https://www.truepeoplesearch.com/find/person/p123", None, fetcher
                )
        parse_person.assert_not_called()
        get_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
