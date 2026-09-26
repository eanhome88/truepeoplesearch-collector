#!/usr/bin/env python3
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tps_queue import make_redis
from tps_coverage import (
    SITE_UNIVERSE,
    dir_kind,
    extract_listings,
    match_slice,
    migrate_seen_to_queued,
    normalize_slice,
    persist_slice,
    reset_stale_pipeline,
    scale_snapshot,
    select_follow_dirs,
)
from tps_queue import QUEUED_KEY, SEEN_KEY, JOB_KEY_PREFIX, feed
from discover import process_page

SURNAME_HTML = """
<div class="card card-body">
  <a href="/find/person/keepco01">Jane Denver</a>
  Age 42<br>
  Denver, CO
</div>
<div class="card card-body">
  <a href="/find/person/skiptx01?city=Houston&amp;state=TX">Sam Houston</a>
  Age 31<br>
  Houston, TX
</div>
<div class="card card-body">
  <a href="/find/person/noage001">Pat Mystery</a>
  Denver, CO
</div>
<a href="/find/adams">Adams</a>
<a href="/find/ahmed">Ahmed</a>
<a href="/find/anderson/denver-co">Denver, CO</a>
<a href="/find/anderson/houston-tx">Houston, TX</a>
"""

LETTER_HTML = """
<a href="/find/adams">Adams</a>
<a href="/find/ahmed">Ahmed</a>
<a href="/find/person/shouldnot">Should Not Feed</a>
"""


class TestCoverage(unittest.TestCase):
    def test_dir_kind(self):
        self.assertEqual(dir_kind("https://www.truepeoplesearch.com/find/a"), "letter")
        self.assertEqual(dir_kind("https://www.truepeoplesearch.com/find/adams"), "surname")
        self.assertEqual(dir_kind("https://www.truepeoplesearch.com/find/adams/denver-co"), "refined")
        self.assertEqual(dir_kind("https://www.truepeoplesearch.com/find/person/abc"), "person")

    def test_extract_listings_and_slice(self):
        cards = extract_listings(SURNAME_HTML, "https://www.truepeoplesearch.com/find/anderson")
        self.assertEqual(len(cards), 3)
        jane = next(c for c in cards if c["person_id"] == "keepco01")
        self.assertEqual(jane["age"], 42)
        self.assertEqual(jane["state"], "CO")
        self.assertEqual(jane["city"], "Denver")
        cfg = normalize_slice("a", "CO", "Denver", None, None)
        self.assertIsNone(match_slice(jane, cfg))
        sam = next(c for c in cards if c["person_id"] == "skiptx01")
        self.assertEqual(match_slice(sam, cfg), "state")
        pat = next(c for c in cards if c["person_id"] == "noage001")
        self.assertEqual(pat["city"], "Denver")
        self.assertEqual(pat["state"], "CO")
        age_cfg = normalize_slice("a", "", "", 30, 50)
        self.assertIsNone(match_slice(pat, age_cfg))
        self.assertEqual(match_slice({"age": 20, "state": "", "city": ""}, age_cfg), "age")

    def test_select_follow_dirs_no_sibling_surnames(self):
        page = "https://www.truepeoplesearch.com/find/anderson"
        dirs = [
            "https://www.truepeoplesearch.com/find/adams",
            "https://www.truepeoplesearch.com/find/ahmed",
            "https://www.truepeoplesearch.com/find/anderson/denver-co",
            "https://www.truepeoplesearch.com/find/anderson/houston-tx",
        ]
        empty = normalize_slice("a")
        self.assertEqual(select_follow_dirs(page, dirs, {"a"}, empty), [])
        co = normalize_slice("a", "CO", "Denver")
        follow = select_follow_dirs(page, dirs, {"a"}, co)
        self.assertEqual(follow, ["https://www.truepeoplesearch.com/find/anderson/denver-co"])

        letter = "https://www.truepeoplesearch.com/find/a"
        letter_follow = select_follow_dirs(letter, dirs, {"a"}, empty)
        self.assertIn("https://www.truepeoplesearch.com/find/adams", letter_follow)
        self.assertNotIn("https://www.truepeoplesearch.com/find/anderson/denver-co", letter_follow)

    def test_process_page_letter_does_not_feed_people(self):
        r = make_redis()
        out = process_page(
            r,
            "https://www.truepeoplesearch.com/find/a",
            LETTER_HTML,
            {"a"},
            normalize_slice("a"),
        )
        self.assertEqual(out["kind"], "letter")
        self.assertEqual(out["enqueued"], 0)
        self.assertGreaterEqual(out["dir_queued"], 1)

    def test_process_page_surname_filters_and_no_siblings(self):
        r = make_redis()
        cfg = normalize_slice("a", "CO", "Denver")
        out = process_page(
            r,
            "https://www.truepeoplesearch.com/find/anderson",
            SURNAME_HTML,
            {"a"},
            cfg,
        )
        self.assertEqual(out["listed"], 3)
        self.assertEqual(out["in_scope"], 2)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(out["enqueued"], 2)
        self.assertEqual(r.llen("tps:pending"), 2)
        self.assertEqual(r.scard(SEEN_KEY), 0)
        self.assertEqual(r.scard(QUEUED_KEY), 2)
        self.assertNotIn("adams", " ".join(out["follow"]))

    def test_migrate_seen_to_queued(self):
        r = make_redis()
        feed(r, ["https://www.truepeoplesearch.com/find/person/abc123def456"])
        # simulate old bug: id already in seen
        job_id = r.lindex("tps:pending", 0)
        raw = r.get(f"{JOB_KEY_PREFIX}{job_id}")
        pid = "abc123def456"
        r.sadd(SEEN_KEY, pid)
        r.delete("tps:cover:migrated")
        moved = migrate_seen_to_queued(r)
        self.assertGreaterEqual(moved, 1)
        self.assertFalse(r.sismember(SEEN_KEY, pid))
        self.assertTrue(r.sismember(QUEUED_KEY, pid))
        self.assertEqual(migrate_seen_to_queued(r), 0)

    def test_reset_stale_pipeline_drops_refined_and_unfiltered_jobs(self):
        r = make_redis()
        persist_slice(r, normalize_slice("a"))
        r.sadd("tps:discover:seen",
            "https://www.truepeoplesearch.com/find/a",
            "https://www.truepeoplesearch.com/find/adams",
            "https://www.truepeoplesearch.com/find/adams/denver-co",
        )
        feed(r, [
            "https://www.truepeoplesearch.com/find/person/flood001",
            "https://www.truepeoplesearch.com/find/person/keepme01",
        ])
        r.sadd("tps:seen", "keepme01")
        out = reset_stale_pipeline(r, kept_person_ids=["keepme01"])
        self.assertGreaterEqual(out["refined_removed"], 1)
        self.assertEqual(r.llen("tps:pending"), 0)
        self.assertEqual(r.scard("tps:queued"), 0)
        self.assertTrue(r.sismember("tps:seen", "keepme01"))
        self.assertFalse(r.sismember("tps:seen", "flood001"))
        seen_dirs = r.smembers("tps:discover:seen")
        self.assertTrue(any("find/adams" in u and "denver" not in u for u in seen_dirs))
        self.assertFalse(any("denver-co" in u for u in seen_dirs))
        pending_dirs = r.lrange("tps:discover:pending", 0, -1)
        self.assertTrue(any(u.endswith("/find/a") for u in pending_dirs))
        self.assertTrue(any(u.endswith("/find/adams") for u in pending_dirs))
        self.assertFalse(any("denver-co" in u for u in pending_dirs))

    def test_scale_snapshot_layers(self):
        r = make_redis()
        persist_slice(r, normalize_slice("a"))
        r.sadd(
            "tps:discover:seen",
            "https://www.truepeoplesearch.com/find/a",
            "https://www.truepeoplesearch.com/find/adams",
            "https://www.truepeoplesearch.com/find/ahmed",
        )
        snap = scale_snapshot(r, persons=24, use_pages=False)
        self.assertEqual(snap["universe"], SITE_UNIVERSE)
        self.assertEqual(SITE_UNIVERSE, 250_000_000)
        self.assertEqual(snap["surnames_indexed"], 2)
        self.assertEqual(snap["directory_slice"], 1000)
        self.assertEqual(snap["persons"], 24)
        self.assertAlmostEqual(snap["pct_of_universe"], 24 / SITE_UNIVERSE)
        self.assertEqual([layer["id"] for layer in snap["layers"]], ["universe", "geo", "directory", "done"])
        self.assertTrue(any(row["letter"] == "a" and row["selected"] for row in snap["letter_rows"]))
        self.assertTrue(any(row["letter"] == "b" and not row["selected"] for row in snap["letter_rows"]))

        ca = scale_snapshot(r, normalize_slice("a", "CA"), persons=24, use_pages=False)
        self.assertLess(ca["universe_slice"], snap["universe_slice"])
        self.assertLess(ca["directory_slice"], snap["directory_slice"])
        self.assertGreater(ca["geo_share"], 0)
        self.assertLess(ca["geo_share"], 1)
        self.assertTrue(any(row["state"] == "CA" and row["selected"] for row in ca["state_rows"]))

        age = scale_snapshot(r, normalize_slice("a", "", "", 18, 29), persons=0, use_pages=False)
        self.assertAlmostEqual(age["age_share"], 0.20, places=2)
        self.assertLess(age["universe_slice"], SITE_UNIVERSE)
        self.assertTrue(any(row["id"] == "18-29" and row["selected"] for row in age["age_rows"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
