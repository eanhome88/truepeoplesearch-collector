"""v2 客户端筛选走前缀和主键翻页，不把整表拉进内存。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "v2"))

import query


class QueryTest(unittest.TestCase):
    def test_phone_prefix_matches_stored_format(self):
        self.assertEqual(query.phone_prefix("303"), "(303%")
        self.assertEqual(query.phone_prefix("303210"), "(303) 210%")
        self.assertEqual(query.phone_prefix("3032109670"), "(303) 210-9670%")

    def test_name_and_phone_are_prefixes(self):
        where, params = query.build_filter("Ann", "ca", "303", True)
        self.assertIn("full_name LIKE %s", where)
        self.assertIn("current_state = %s", where)
        self.assertIn("phone_number LIKE %s", where)
        self.assertIn("primary_phone_type = %s", where)
        self.assertIn("Ann%", params)
        self.assertIn("CA", params)
        self.assertIn("(303%", params)
        self.assertNotIn("%Ann%", params)

    def test_page_uses_person_id_keyset(self):
        where, _params = query.build_filter("", "", "", False)
        sql, extra = query.page_sql(where, "px200")
        self.assertIn("person_id < %s", sql)
        self.assertIn("ORDER BY person_id DESC", sql)
        self.assertNotIn("OFFSET", sql)
        self.assertEqual(extra, ["px200"])
        self.assertLessEqual(query.PAGE_SIZE, 100)


if __name__ == "__main__":
    unittest.main()
