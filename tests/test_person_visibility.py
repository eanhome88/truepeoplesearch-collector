"""Read eligibility uses an in-memory SQL database and synthetic fixtures only."""

import csv
import importlib.util
import io
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from person_visibility import person_export_view_sql, person_has_phone_sql, usable_phone_sql


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.dict(os.environ, {}, clear=True), patch("tps_env.load_project_env"):
    api = load_module("dashboard_visibility_test", ROOT / "tools" / "dashboard_api.py")
exporter = load_module("csv_visibility_test", ROOT / "tools" / "export_csv.py")


class PersonVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("regexp", 2, lambda pattern, value: bool(re.search(pattern, value or "")))
        self.db.create_function("CONCAT", -1, lambda *values: None if any(value is None for value in values) else "".join(map(str, values)))
        self.db.create_function("CONCAT_WS", -1, lambda separator, *values: str(separator).join(str(value) for value in values if value is not None))
        self.db.executescript("""
            CREATE TABLE persons (
                person_id TEXT PRIMARY KEY, full_name TEXT, first_name TEXT, middle_name TEXT,
                last_name TEXT, gender TEXT, age INTEGER, birth_year INTEGER,
                primary_phone TEXT, primary_phone_type TEXT,
                wireless_phone_1 TEXT, wireless_phone_2 TEXT, wireless_phone_3 TEXT,
                current_city TEXT, current_state TEXT, marital_status TEXT,
                current_address TEXT, address_duration TEXT, all_phones TEXT,
                phone_count INTEGER, email_count INTEGER, prev_addr_count INTEGER,
                alias_count INTEGER, scraped_at TEXT
            );
            CREATE TABLE phone_numbers (person_id TEXT, phone_number TEXT, line_type TEXT,
                carrier TEXT, is_primary INTEGER);
            CREATE UNIQUE INDEX uk_phones ON phone_numbers (person_id, phone_number);
            CREATE TABLE email_addresses (person_id TEXT, email TEXT);
            CREATE TABLE aliases (person_id TEXT, alias_name TEXT);
            CREATE TABLE current_addresses (person_id TEXT, street TEXT);
            CREATE TABLE previous_addresses (person_id TEXT, street TEXT);
        """)
        fixtures = [
            ("mobile", "(202) 555-0101", "Wireless", None, None, 1),
            ("landline", "202.555.0102", "Landline", None, None, 1),
            ("child_only", None, None, None, None, 0),
            ("third_mobile", None, None, "2025550104", None, 1),
            ("unknown_type", "+1 202 555 0105", None, None, None, 1),
            ("services", "+1 202 555 0107", "Landline/Services", None, None, 1),
            ("voip", "2025550108", "VoIP", None, None, 1),
            ("child_unknown", None, None, None, None, 1),
            ("child_voip", None, None, None, None, 1),
            ("repeated", "2222222222", "Wireless", None, None, 1),
            ("bad_exchange", "2021550101", "Landline", None, None, 1),
            ("masked", "202***0101", "Wireless", None, None, 1),
            ("normalized_type", "2025550109", " wIrElEsS ", None, None, 1),
            ("mixed", "2025550190", "VoIP", "2025550112", "2025550190 (VoIP), 2025550191 (Unknown)", 3),
            ("empty", "", "Wireless", " ", None, 99),
            ("none", None, None, None, None, 99),
            ("invalid", "not a number", None, None, None, 99),
            ("letters", "Phone 2025550123", "Wireless", None, None, 1),
            ("short", "123456", None, None, None, 99),
            ("long", "1234567890123456", None, None, None, 99),
            ("text_only", None, None, None, "2025550106", 99),
        ]
        for pid, phone, kind, third, text, count in fixtures:
            self.db.execute("""INSERT INTO persons
                (person_id, full_name, primary_phone, primary_phone_type, wireless_phone_3,
                 all_phones, phone_count, email_count, prev_addr_count, alias_count,
                 current_city, current_state, age, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 0, 'Synthetic City', 'ZZ', 42, '2026-01-01')
            """, (pid, "Synthetic " + pid, phone, kind, third, text, count))
            self.db.execute("INSERT INTO email_addresses VALUES (?, ?)", (pid, pid + "@example.invalid"))
        self.db.execute("INSERT INTO phone_numbers VALUES ('child_only', '202-555-0103', 'Landline', '', 1)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('child_unknown', '202-555-0110', NULL, '', 1)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('child_voip', '202-555-0111', 'VoIP', '', 1)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('mixed', '2025550190', 'VoIP', '', 1)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('mixed', '2025550191', NULL, '', 0)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('mixed', '2025550112', 'Wireless', '', 0)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('invalid', '-', 'Wireless', '', 1)")
        self.total = len(fixtures)
        self.visible = {"mobile", "landline", "child_only", "third_mobile", "services", "normalized_type", "mixed"}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.query_mock = self.stack.enter_context(patch.object(api, "query", side_effect=self.query))
        self.query_one_mock = self.stack.enter_context(patch.object(api, "query_one", side_effect=self.query_one))
        self.stack.enter_context(patch.object(api, "get_redis", return_value=None))
        self.stack.enter_context(patch.object(api, "try_set_tiflash_read"))
        self.client = api.app.test_client()

    def query(self, sql, params=None):
        return [dict(row) for row in self.db.execute(sql.replace("%s", "?"), params or [])]

    def query_one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def assert_visible(self, rows, field="person_id"):
        self.assertEqual({row[field] for row in rows}, self.visible)

    def test_shared_predicate_accepts_mobile_landline_and_child_only_without_mutation(self):
        rows = self.query(f"SELECT p.person_id FROM persons p WHERE {person_has_phone_sql()}")
        self.assert_visible(rows)
        self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM persons")["n"], self.total)
        for value in ("p; DROP TABLE persons", "p.phone OR 1=1", "a b"):
            with self.assertRaises(ValueError):
                person_has_phone_sql(value)
            with self.assertRaises(ValueError):
                usable_phone_sql(value)

    def test_listing_total_and_pagination_exclude_legacy_rows(self):
        response = self.client.get("/api/persons?size=200")
        self.assertEqual(response.status_code, 200)
        self.assert_visible(response.json["data"])
        self.assertEqual(response.json["total"], len(self.visible))
        page = self.client.get("/api/persons?size=2&page=2").json
        self.assertEqual(len(page["data"]), 2)
        self.assertEqual(page["total"], len(self.visible))
        wireless = self.client.get("/api/persons?has_wireless=1&size=200").json
        self.assertEqual({row["person_id"] for row in wireless["data"]}, {"mobile", "third_mobile", "normalized_type", "mixed"})
        self.assertEqual(wireless["with_wireless"], 4)

    def test_sql_eligibility_matches_the_ingestion_rule(self):
        with patch.dict(os.environ, {}, clear=True), patch("tps_env.load_project_env"):
            scraper = load_module("scraper_visibility_rules", ROOT / "scripts" / "scrape_to_tidb.py")
        for row in self.query("SELECT * FROM persons"):
            row["phone_numbers"] = self.query("SELECT * FROM phone_numbers WHERE person_id = ?", (row["person_id"],))
            self.assertEqual(scraper.has_usable_phone(row), row["person_id"] in self.visible, row["person_id"])

    def test_phone_type_filters_include_all_visible_contacts_and_match_csv(self):
        cases = (
            ("Wireless", {"mobile", "third_mobile", "normalized_type", "mixed"}),
            (" wIrElEsS ", {"mobile", "third_mobile", "normalized_type", "mixed"}),
            ("Landline", {"landline", "child_only", "services"}),
            ("Landline/Services", {"services"}),
        )
        for kind, expected in cases:
            with self.subTest(phone_type=kind):
                response = self.client.get("/api/persons", query_string={"phone_type": kind, "size": 200})
                self.assertEqual(response.status_code, 200)
                self.assertEqual({row["person_id"] for row in response.json["data"]}, expected)
                self.assertEqual(response.json["total"], len(expected))
                page = self.client.get("/api/persons", query_string={"phone_type": kind, "size": 1, "page": 1})
                self.assertEqual(len(page.json["data"]), 1)
                self.assertEqual(page.json["total"], len(expected))
                for endpoint in ("/api/export", "/api/persons/export"):
                    exported = self.client.get(endpoint, query_string={"phone_type": kind})
                    self.assertEqual(exported.status_code, 200)
                    rows = csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig")))
                    self.assertEqual({row["人物ID"] for row in rows}, expected)

    def test_type_filter_requires_a_usable_contact_and_can_match_secondary_phone(self):
        self.db.execute("UPDATE persons SET primary_phone='-', primary_phone_type='Landline' WHERE person_id='mixed'")
        response = self.client.get("/api/persons?phone_type=Landline&size=200")
        self.assertNotIn("mixed", {row["person_id"] for row in response.json["data"]})
        self.db.execute("INSERT INTO phone_numbers VALUES ('mixed', '2025550113', ' Landline/Services ', '', 0)")
        response = self.client.get("/api/persons?phone_type=Landline&size=200")
        self.assertIn("mixed", {row["person_id"] for row in response.json["data"]})
        response = self.client.get("/api/persons?phone_type=Wireless&size=200")
        self.assertIn("mixed", {row["person_id"] for row in response.json["data"]})

    def test_noneligible_or_invalid_phone_type_is_rejected_before_sql(self):
        for kind in ("VoIP", "Unknown", "Wireless' OR 1=1 --"):
            for endpoint in ("/api/persons", "/api/export", "/api/persons/export"):
                with self.subTest(phone_type=kind, endpoint=endpoint):
                    response = self.client.get(endpoint, query_string={"phone_type": kind})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("电话类型", response.json["error"])
        self.query_mock.assert_not_called()
        self.query_one_mock.assert_not_called()

    def test_formatted_and_digits_only_phone_filters_return_the_same_contacts(self):
        cases = (
            ("2025550101", "mobile"),
            ("(202) 555-0101", "mobile"),
            ("12025550101", "mobile"),
            ("+1 (202) 555-0101", "mobile"),
            ("2025550102", "landline"),
            ("2025550103", "child_only"),
            ("2025550104", "third_mobile"),
            ("2025550107", "services"),
            ("5550101", "mobile"),
            ("0101", "mobile"),
        )
        for number, expected in cases:
            with self.subTest(number=number):
                response = self.client.get("/api/persons", query_string={"phone": number})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["person_id"] for row in response.json["data"]], [expected])
                self.assertEqual(response.json["total"], 1)
                for endpoint in ("/api/export", "/api/persons/export"):
                    exported = self.client.get(endpoint, query_string={"phone": number})
                    self.assertEqual(exported.status_code, 200)
                    rows = list(csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig"))))
                    self.assertEqual([row["人物ID"] for row in rows], [expected])

    def test_phone_filter_cannot_match_hidden_voip_unknown_or_unqualified_contacts(self):
        for number in ("2025550190", "2025550191", "2025550105", "2025550108", "2025550110", "2025550111", "2025550106"):
            with self.subTest(number=number):
                response = self.client.get("/api/persons", query_string={"phone": number})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["data"], [])
                self.assertEqual(response.json["total"], 0)
                exported = self.client.get("/api/export", query_string={"phone": number})
                self.assertEqual(list(csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig")))), [])
                searched = self.client.get("/api/search", query_string={"q": number}).json
                self.assertEqual(searched["phones"], [])
                self.assertEqual(searched["persons"], [])

    def test_global_phone_search_normalizes_formats_country_code_and_partial_digits(self):
        for number in ("2025550103", "12025550103", "+1 (202) 555-0103", "5550103", "0103"):
            with self.subTest(number=number):
                response = self.client.get("/api/search", query_string={"q": number})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["person_id"] for row in response.json["phones"]], ["child_only"])
        for number, expected in (("2025550101", "mobile"), ("+1 (202) 555-0104", "third_mobile"), ("2025550107", "services")):
            with self.subTest(primary_or_wireless_only=expected):
                response = self.client.get("/api/search", query_string={"q": number})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["person_id"] for row in response.json["persons"]], [expected])

    def test_phone_search_parameters_stay_bound_and_child_lookup_uses_person_index(self):
        match, values = api._phone_match_sql("p.primary_phone", "2025550101")
        self.assertNotIn("REGEXP", match)
        self.assertNotIn("2025550101", match)
        self.assertNotIn("LIKE", match)
        self.assertEqual(values, ["2025550101", "12025550101"])
        self.client.get("/api/persons?phone=2025550103")
        sql, params = self.query_mock.call_args.args
        plan = self.query("EXPLAIN QUERY PLAN " + sql, params)
        details = "\n".join(row["detail"] for row in plan)
        self.assertIn("SEARCH matched_phone USING", details)
        self.assertIn("uk_phones", details)
        self.assertNotIn("SCAN matched_phone", details)
        for number in ("%", "_", "2025550101' OR 1=1 --", "123456789012"):
            with self.subTest(number=number):
                self.query_mock.reset_mock()
                self.query_one_mock.reset_mock()
                response = self.client.get("/api/persons", query_string={"phone": number})
                self.assertEqual(response.status_code, 400)
                self.query_mock.assert_not_called()
                self.query_one_mock.assert_not_called()

    def test_hidden_detail_returns_404_without_loading_child_records(self):
        response = self.client.get("/api/person/none")
        self.assertEqual(response.status_code, 404)
        self.query_mock.assert_not_called()
        response = self.client.get("/api/person/child_only")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["person"]["person_id"], "child_only")

    def test_search_recent_city_and_age_use_same_eligibility(self):
        result = self.client.get("/api/search?q=Synthetic").json
        self.assert_visible(result["persons"])
        result = self.client.get("/api/search?q=example.invalid").json
        self.assert_visible(result["emails"])
        self.assert_visible(self.client.get("/api/recent?limit=100").json)
        self.assertEqual(self.client.get("/api/cities").json[0]["cnt"], len(self.visible))
        self.assertEqual(self.client.get("/api/age-distribution").json[0]["cnt"], len(self.visible))

    def test_stats_counts_only_eligible_stock_and_legacy_fallback_remains_filtered(self):
        stats = api._load_stats()
        self.assertEqual(stats["persons"], len(self.visible))
        self.assertEqual(stats["phones"], len(self.visible))
        self.assertEqual(stats["wireless_count"], 4)
        self.query_one_mock.side_effect = [RuntimeError("synthetic old schema"), self.query_one(
            f"SELECT COUNT(*) AS persons FROM persons p WHERE {person_has_phone_sql(legacy=True)}"
        ), {"phones": 2}]
        stats = api._load_stats()
        self.assertEqual(stats["persons"], 2)
        self.assertEqual(stats["phones"], 2)
        self.assertIn(person_has_phone_sql(legacy=True), self.query_one_mock.call_args.args[0])

    def test_qualified_phone_count_deduplicates_person_fields_and_child_formats(self):
        self.db.execute("UPDATE persons SET wireless_phone_1='1-202-555-0101', "
                        "wireless_phone_2='202 555 0120', wireless_phone_3='(202) 555-0120', "
                        "phone_count=99 WHERE person_id='mobile'")
        self.db.execute("INSERT INTO phone_numbers VALUES ('mobile', '+1 (202) 555-0101', 'Wireless', '', 0)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('mobile', '202.555.0120', 'Wireless', '', 0)")
        self.db.execute("INSERT INTO phone_numbers VALUES ('mobile', '2025550130', 'VoIP', '', 0)")
        listed = self.client.get("/api/persons?size=200").json["data"]
        counts = {row["person_id"]: row["phone_count"] for row in listed}
        self.assertEqual(counts["mobile"], 2)
        self.assertTrue(all(count == 1 for pid, count in counts.items() if pid != "mobile"))
        self.assertEqual(self.client.get("/api/person/mobile").json["person"]["phone_count"], 2)
        recent = {row["person_id"]: row["phone_count"] for row in self.client.get("/api/recent?limit=100").json}
        self.assertEqual(recent["mobile"], 2)
        stats = api._load_stats()
        self.assertEqual(stats["phones"], len(self.visible) + 1)
        self.assertEqual(self.query_one("SELECT phone_count FROM persons WHERE person_id='mobile'")["phone_count"], 99)

    def test_phone_total_failure_is_unavailable_not_a_partial_child_count(self):
        self.query_one_mock.side_effect = [
            {"persons": len(self.visible), "emails": 0, "prev_addr": 0, "aliases": 0,
             "wireless_count": 4, "primary_wireless_count": 3, "smart_fallback_count": 0},
            RuntimeError("synthetic count failure"),
        ]
        stats = api._load_stats()
        self.assertTrue(stats["database_available"])
        self.assertIsNone(stats["phones"])

    def test_large_stock_does_not_run_expensive_exact_total_on_overview(self):
        self.query_one_mock.return_value = {"persons": 1000000, "emails": 0, "prev_addr": 0,
                                            "aliases": 0, "wireless_count": 0,
                                            "primary_wireless_count": 0, "smart_fallback_count": 0}
        self.query_one_mock.side_effect = None
        stats = api._load_stats()
        self.assertTrue(stats["database_available"])
        self.assertIsNone(stats["phones"])
        self.assertEqual(self.query_one_mock.call_count, 1)

    def test_web_csv_hides_no_phone_records(self):
        for endpoint in ("/api/export", "/api/persons/export"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))
            self.assert_visible(rows, "人物ID")

    def test_mixed_records_never_expose_voip_or_unknown_contact_fields(self):
        detail = self.client.get("/api/person/mixed")
        self.assertEqual(detail.status_code, 200)
        person = detail.json["person"]
        self.assertEqual(person["primary_phone"], "2025550112")
        self.assertEqual(person["primary_phone_type"], "Wireless")
        self.assertEqual([phone["phone_number"] for phone in detail.json["phone_numbers"]], ["2025550112"])
        for endpoint in ("/api/persons?size=200", "/api/recent?limit=100", "/api/export", "/api/persons/export"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            body = response.data.decode("utf-8-sig")
            self.assertNotIn("2025550190", body)
            self.assertNotIn("2025550191", body)
        stored = self.query_one("SELECT primary_phone, all_phones FROM persons WHERE person_id='mixed'")
        self.assertEqual(stored["primary_phone"], "2025550190")
        self.assertIn("2025550191", stored["all_phones"])

    def test_page_projection_uses_indexed_child_lookups_after_result_limit(self):
        self.client.get("/api/persons?size=2&page=2")
        sql, params = self.query_mock.call_args.args
        self.assertIn("LIMIT %s OFFSET %s\n            ) p", sql)
        plan = self.query("EXPLAIN QUERY PLAN " + sql, params)
        details = "\n".join(row["detail"] for row in plan)
        self.assertIn("SEARCH visible_phone USING", details)
        self.assertIn("uk_phones", details)
        self.assertNotIn("SCAN visible_phone", details)

    def test_cli_csv_uses_the_same_real_sql_predicate(self):
        cursor = MagicMock()
        result = []
        cursor.execute.side_effect = lambda sql: result.extend(self.query(sql))
        def fetchmany(size):
            batch = result[:size]
            del result[:size]
            return batch
        cursor.fetchmany.side_effect = fetchmany
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with tempfile.TemporaryDirectory() as directory, patch.object(exporter, "get_db", return_value=connection), redirect_stdout(io.StringIO()):
            output = Path(directory) / "synthetic.csv"
            exporter.export_leads(str(output))
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
                self.assert_visible(rows, "人物ID")
                mixed = next(row for row in rows if row["人物ID"] == "mixed")
                self.assertEqual(mixed["当前电话"], "2025550112")
                self.assertNotIn("2025550190", mixed["电话列表"])
                self.assertNotIn("2025550191", mixed["电话列表"])
        self.assertIn(person_has_phone_sql(), cursor.execute.call_args.args[0])

    def test_pipeline_count_recent_and_plan_read_only_aggregates_are_filtered(self):
        with patch.object(api, "get_redis", return_value=MagicMock()), \
                patch("tps_control.pipeline_status", return_value={}), \
                patch("tps_coverage.scale_snapshot", return_value={}), \
                patch("tps_plan.load_plan", return_value={"lanes": 1, "inflight": 1, "page_sec": 1}), \
                patch("tps_plan.build_plan", return_value={}), \
                patch("proxy_pool.load_proxy_config", return_value={}):
            payload = api._pipeline_payload(read_only=True)
        self.assertEqual(payload["persons"], len(self.visible))
        self.assert_visible(payload["recent"])

    def test_generated_and_shipped_views_filter_without_deleting_old_rows(self):
        statements = [person_export_view_sql()]
        for path in (ROOT / "sql" / "tidb_schema.sql", ROOT / "sql" / "v_export_leads.sql"):
            source = path.read_text(encoding="utf-8")
            statements.append(source[source.index("CREATE OR REPLACE VIEW 人物主表 AS"):].split(";", 1)[0])
        for statement in statements:
            self.db.execute("DROP VIEW IF EXISTS 人物主表")
            self.db.execute(statement.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))
            self.assert_visible(self.query("SELECT * FROM 人物主表"), "人物ID")
            mixed = self.query_one("SELECT * FROM 人物主表 WHERE 人物ID='mixed'")
            self.assertEqual(mixed["当前电话"], "2025550112")
            self.assertNotIn("2025550190", mixed["电话列表"])
            self.assertNotIn("2025550191", mixed["电话列表"])
            self.assertEqual(self.query_one("SELECT COUNT(*) AS n FROM persons")["n"], self.total)
        self.assertNotIn("DELETE FROM", (ROOT / "sql" / "v_export_leads.sql").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
