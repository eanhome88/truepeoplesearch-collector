"""Demonstration output must never mutate real data or metrics."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


class OfflineDemoTests(unittest.TestCase):
    def test_import_and_legacy_entrypoint_never_connect_to_services(self):
        path = Path(__file__).resolve().parents[1] / "tools" / "seed_test_data.py"
        with patch("mysql.connector.connect", side_effect=AssertionError("DB forbidden")), \
                patch("redis.Redis", side_effect=AssertionError("Redis forbidden")), \
                patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            spec = importlib.util.spec_from_file_location("offline_demo", path)
            demo = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(demo)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                demo.seed_data()
        fixture = json.loads(output.getvalue())
        self.assertTrue(fixture["demo"])
        self.assertEqual(fixture["stats"]["metrics_quality"]["status"], "demo")
        self.assertIsNone(fixture["stats"]["traffic_saved_gb"])
        self.assertNotIn("SAMPLE_PERSONS", vars(demo))
        self.assertNotIn("DB_CONFIG", vars(demo))


if __name__ == "__main__":
    unittest.main()
