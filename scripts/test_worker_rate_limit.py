#!/usr/bin/env python3
"""429 暂停领取：不增加 attempts，同一轮限流不升级。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tps_queue import URL, make_redis  # noqa: E402
from tps_metrics import COUNTER_KEY  # noqa: E402
from tps_queue import claim, feed, queue_stats  # noqa: E402
from distributed_worker import (  # noqa: E402
    LeaseWorker,
    classify_error,
    plan_chrome_groups,
    rate_limit_pause_sec,
)
from proxy_pool import StickyLanes  # noqa: E402
from scrape_to_tidb import HttpError  # noqa: E402


class TestRateLimitPause(unittest.TestCase):
    def test_two_ips_split_one_chrome_each(self):
        self.assertEqual(plan_chrome_groups(2, 2), [1, 1])
        self.assertEqual(plan_chrome_groups(8, 2), [4, 4])
        self.assertEqual(plan_chrome_groups(2, 0), [2])

    def test_429_switches_ip_without_pausing_the_process(self):
        r = make_redis()
        feed(r, [URL])
        job = claim(r, "worker-ip")
        lanes = StickyLanes(["http://10.0.0.1:8000", "http://10.0.0.2:8000"], rest_sec=4200)
        worker = LeaseWorker(r, 1, 1000, 8.0, lanes=lanes)
        group = worker.groups[0]
        self.assertEqual(group.proxy, "http://10.0.0.1:8000")
        slot = worker.slots[0]
        slot.job = job
        slot.jid = job["id"]
        worker.in_flight[job["id"]] = job
        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": job["id"],
            "bucket": "rate_limit",
            "error": "HTTP 429 for " + URL,
            "generation": 0,
            "person": job["person_id"],
            "url": job["url"],
        })
        self.assertEqual(group.proxy, "http://10.0.0.2:8000")
        self.assertEqual(group.generation, 1)
        self.assertEqual(worker._pause_remaining_sec(), 0)
        self.assertEqual(worker._rate_limit_streak, 0)
        self.assertEqual(queue_stats(r)["pending"], 1)
        self.assertEqual(queue_stats(r)["dlq"], 0)

        again = claim(r, "worker-ip-2")
        slot.job = again
        slot.jid = again["id"]
        worker.in_flight[again["id"]] = again
        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": again["id"],
            "bucket": "rate_limit",
            "error": "HTTP 429",
            "generation": 1,
            "person": again["person_id"],
            "url": again["url"],
        })
        self.assertEqual(group.proxy, "http://10.0.0.2:8000")
        self.assertGreater(group.claim_after, time.monotonic())
        self.assertEqual(worker._pause_remaining_sec(), 0)
        self.assertEqual(int(claim(r, "worker-ip-3").get("attempts") or 0), 0)

    def test_pause_steps(self):
        self.assertEqual(rate_limit_pause_sec(1), 300)
        self.assertEqual(rate_limit_pause_sec(2), 900)
        self.assertEqual(rate_limit_pause_sec(3), 2700)
        self.assertEqual(rate_limit_pause_sec(9), 2700)

    def test_classify_429_before_generic_4xx(self):
        self.assertEqual(classify_error(HttpError(429, "HTTP 429 for https://example")), "rate_limit")
        self.assertEqual(classify_error(HttpError(404, "HTTP 404 for https://example")), "http_4xx")
        self.assertEqual(classify_error(RuntimeError("HTTP 429 for https://example")), "rate_limit")

    def test_done_releases_and_pauses_without_dlq(self):
        r = make_redis()
        feed(r, [URL])
        job = claim(r, "worker-429")
        worker = LeaseWorker(r, 1, 1000, 8.0)
        slot = worker.slots[0]
        slot.job = job
        slot.jid = job["id"]
        worker.in_flight[job["id"]] = job

        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": job["id"],
            "bucket": "rate_limit",
            "error": "HTTP 429 for " + URL,
            "scrape_ms": 1200,
            "person": job["person_id"],
            "url": job["url"],
        })

        stats = queue_stats(r)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats.get("dlq", 0), 0)
        self.assertGreater(worker._pause_remaining_sec(), 290)
        self.assertEqual(worker._rate_limit_streak, 1)
        self.assertEqual(worker._heartbeat_status(), "paused")
        self.assertEqual(int(r.get(COUNTER_KEY.format(bucket="rate_limit")) or 0), 1)

        again = claim(r, "worker-429-again")
        self.assertEqual(int(again.get("attempts") or 0), 0)
        slot.job = again
        slot.jid = again["id"]
        worker.in_flight[again["id"]] = again
        before = worker._claim_after
        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": again["id"],
            "bucket": "rate_limit",
            "error": "HTTP 429",
            "person": again["person_id"],
            "url": again["url"],
        })
        self.assertEqual(worker._rate_limit_streak, 1)
        self.assertEqual(worker._claim_after, before)
        self.assertEqual(queue_stats(r)["pending"], 1)
        self.assertEqual(queue_stats(r)["processing"], 0)
        self.assertEqual(int(claim(r, "worker-429-third").get("attempts") or 0), 0)

        worker._claim_after = time.monotonic() - 1
        worker._note_success()
        self.assertEqual(worker._rate_limit_streak, 0)
        self.assertEqual(worker._heartbeat_status(), "running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
