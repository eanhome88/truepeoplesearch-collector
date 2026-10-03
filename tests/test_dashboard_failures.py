"""Dashboard validation/failure contracts, with no database or Redis access."""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


with patch.dict(os.environ, {}, clear=True):
    spec = importlib.util.spec_from_file_location(
        "dashboard_failure_test_api",
        Path(__file__).resolve().parents[1] / "tools" / "dashboard_api.py",
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)


class DashboardFailureTests(unittest.TestCase):
    def setUp(self):
        self.client = api.app.test_client()

    def test_invalid_age_is_400_before_any_query(self):
        invalid = (
            {"age_min": "abc"}, {"age_max": "abc"},
            {"age_min": "-1"}, {"age_max": "121"},
            {"age_min": "1.5"}, {"age_max": "1e2"},
            {"age_min": "9" * 5000}, {"age_min": "65", "age_max": "30"},
        )
        with patch.object(api, "query") as query, patch.object(api, "query_one") as query_one:
            for endpoint in ("/api/persons", "/api/export", "/api/persons/export"):
                for params in invalid:
                    with self.subTest(endpoint=endpoint, params=str(params)[:80]):
                        response = self.client.get(endpoint, query_string=params)
                        self.assertEqual(response.status_code, 400)
                        self.assertIn("年龄", response.json["error"])
                        self.assertNotIn("abc", response.json["error"])
            query.assert_not_called()
            query_one.assert_not_called()

    def test_valid_age_boundaries_keep_parameters_aligned(self):
        where, params, _ = api._build_persons_filter({"age_min": "0", "age_max": "120"})
        self.assertEqual(where, [api.person_has_phone_sql(), "p.age >= %s", "p.age <= %s"])
        self.assertEqual(params, [0, 120])
        where, params, _ = api._build_persons_filter({"age_min": " ", "age_max": ""})
        self.assertEqual(where, [api.person_has_phone_sql()])
        self.assertEqual(params, [])

    def test_read_failures_return_generic_json_not_exception_details(self):
        private_error = "synthetic database error: password=DO_NOT_LEAK"
        with patch.object(api, "query", side_effect=RuntimeError(private_error)):
            for endpoint in ("/api/persons", "/api/export", "/api/persons/export", "/api/search?q=synthetic"):
                with self.subTest(endpoint=endpoint):
                    response = self.client.get(endpoint)
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json, {"error": api.PUBLIC_INTERNAL_ERROR})
                    self.assertNotIn("DO_NOT_LEAK", response.get_data(as_text=True))

    def test_count_query_failure_is_not_reported_as_an_empty_list(self):
        with patch.object(api, "query", return_value=[]), \
             patch.object(api, "query_one", side_effect=RuntimeError("synthetic count failure")):
            response = self.client.get("/api/persons")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json, {"error": api.PUBLIC_INTERNAL_ERROR})

    def test_valid_empty_result_remains_successful(self):
        with patch.object(api, "query", return_value=[]), \
             patch.object(api, "query_one", return_value={"cnt": 0}):
            response = self.client.get("/api/persons?age_min=30&age_max=65")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"], [])
        self.assertEqual(response.json["total"], 0)


if __name__ == "__main__":
    unittest.main()
