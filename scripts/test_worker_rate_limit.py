#!/usr/bin/env python3
"""429 暂停领取：不增加 attempts，同一轮限流不升级。"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

os.environ["TPS_PROXY_PROBE"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tps_queue import URL, make_redis  # noqa: E402
from tps_metrics import COUNTER_KEY  # noqa: E402
from tps_queue import claim, feed, queue_stats  # noqa: E402
from distributed_worker import (  # noqa: E402
    LeaseWorker,
    classify_error,
    plan_chrome_groups,
    proxy_tcp_open,
    proxy_upstream_open,
    rate_limit_pause_sec,
)
from proxy_pool import StickyLanes  # noqa: E402
from protocol_worker import ProtocolWorker  # noqa: E402
from scrape_to_tidb import HttpError  # noqa: E402


class TestRateLimitPause(unittest.TestCase):
    def test_two_ips_split_one_chrome_each(self):
        self.assertEqual(plan_chrome_groups(2, 2), [1, 1])
        self.assertEqual(plan_chrome_groups(8, 2), [4, 4])
        self.assertEqual(plan_chrome_groups(2, 0), [2])

    def test_429_pauses_all_claims_without_switching_proxy(self):
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
        self.assertGreater(worker._pause_remaining_sec(), 290)
        self.assertEqual(worker._rate_limit_streak, 1)

    def test_pause_steps(self):
        self.assertEqual(rate_limit_pause_sec(1), 300)
        self.assertEqual(rate_limit_pause_sec(2), 900)
        self.assertEqual(rate_limit_pause_sec(3), 2700)
        self.assertEqual(rate_limit_pause_sec(9), 2700)

    def test_protocol_no_phone_is_terminal_not_success(self):
        r = make_redis()
        feed(r, [URL])
        job = claim(r, "protocol-quality")
        worker = ProtocolWorker(r, decoupled_ingest=True)
        worker._on_batch_failure(job, ValueError("no_phone"))
        self.assertEqual(queue_stats(r)["pending"], 0)
        self.assertEqual(queue_stats(r)["dlq"], 1)
        self.assertEqual(worker._completed_count, 0)
        self.assertEqual(worker._committed_count, 0)

    def test_browser_no_phone_is_terminal_not_success(self):
        r = make_redis()
        feed(r, [URL])
        job = claim(r, "browser-quality")
        worker = LeaseWorker(r, 1, 1000, 8.0)
        slot = worker.slots[0]
        slot.job = job
        slot.jid = job["id"]
        worker.in_flight[job["id"]] = job
        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": job["id"],
            "bucket": "no_phone",
            "generation": 0,
        })
        self.assertEqual(queue_stats(r)["pending"], 0)
        self.assertEqual(queue_stats(r)["dlq"], 1)
        self.assertEqual(int(r.get(COUNTER_KEY.format(bucket="success")) or 0), 0)

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

    def test_dynamic_gateway_429_rests_only_that_ip(self):
        """穿云一条出口 429 只休本组，不把全部 IP 停掉。"""
        r = make_redis()
        feed(r, [URL])
        job = claim(r, "worker-cb")
        cb_proxy = "http://user-res_US:secret@gw-res.cloudbypass.com:1288"
        lanes = StickyLanes([cb_proxy, "http://user-res_US_s2:secret@gw-res.cloudbypass.com:1288"], rest_sec=0)
        worker = LeaseWorker(r, 1, 1000, 8.0, lanes=lanes)
        group = worker.groups[0]
        group.proxy = cb_proxy

        slot = worker.slots[0]
        slot.job = job
        slot.jid = job["id"]
        worker.in_flight[job["id"]] = job
        now = time.monotonic()

        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": job["id"],
            "bucket": "rate_limit",
            "error": "HTTP 429 for " + URL,
            "scrape_ms": 500,
            "person": job["person_id"],
            "url": job["url"],
            "generation": 0,
        })

        self.assertEqual(worker._pause_remaining_sec(), 0)
        self.assertEqual(worker._heartbeat_status(), "running")
        self.assertGreater(group.claim_after, now)
        self.assertEqual(queue_stats(r)["pending"], 1)

    def test_captcha_keeps_lane_restarts_fingerprint(self):
        r = make_redis()
        feed(r, [URL])
        job = claim(r, "worker-cap")
        lanes = StickyLanes(["http://10.0.0.1:8000", "http://10.0.0.2:8000"], rest_sec=4200)
        worker = LeaseWorker(r, 1, 1000, 8.0, lanes=lanes)
        group = worker.groups[0]
        slot = worker.slots[0]
        slot.job = job
        slot.jid = job["id"]
        worker.in_flight[job["id"]] = job
        now = time.monotonic()
        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": job["id"],
            "bucket": "rate_limit",
            "error": "captcha challenge for " + URL,
            "generation": 0,
            "person": job["person_id"],
            "url": job["url"],
        })
        self.assertEqual(group.proxy, "http://10.0.0.1:8000")
        self.assertEqual(group.generation, 0)
        self.assertEqual(worker._pause_remaining_sec(), 0)
        self.assertGreater(group.claim_after, now)

    def test_site_captcha_rotates_proxy_without_global_pause(self):
        r = make_redis()
        feed(r, [URL])
        job = claim(r, "worker-rot")
        lanes = StickyLanes(["http://10.0.0.1:8000", "http://10.0.0.2:8000"], rest_sec=4200)
        worker = LeaseWorker(r, 1, 1000, 8.0, lanes=lanes)
        group = worker.groups[0]
        slot = worker.slots[0]
        slot.job = job
        slot.jid = job["id"]
        worker.in_flight[job["id"]] = job
        worker._on_done(slot, {
            "slot": 0,
            "kind": "done",
            "id": job["id"],
            "bucket": "rate_limit",
            "cf_route": "rotate_proxy",
            "cf_kind": "site_captcha",
            "error": "rotate_proxy kind=site_captcha",
            "generation": group.generation,
            "person": job["person_id"],
            "url": job["url"],
        })
        self.assertEqual(group.proxy, "http://10.0.0.2:8000")
        self.assertEqual(worker._pause_remaining_sec(), 0)

    def test_second_account_hops_off_fused_account(self):
        r = make_redis()
        lanes = StickyLanes([
            "http://userA:pw@10.0.0.1:8000",
            "http://userB:pw@10.0.0.2:8000",
        ], rest_sec=4200)
        worker = LeaseWorker(r, 1, 1000, 8.0, lanes=lanes)
        group = worker.groups[0]
        self.assertIn("userA", group.proxy)
        self.assertTrue(worker._hop_account(group))
        self.assertIn("userB", group.proxy)
        self.assertEqual(worker._pause_remaining_sec(), 0)

    def test_proxy_probe_skips_closed_port_when_enabled(self):
        os.environ["TPS_PROXY_PROBE"] = "1"
        try:
            self.assertFalse(proxy_tcp_open("http://127.0.0.1:1", timeout=0.2))
            self.assertFalse(proxy_upstream_open("http://127.0.0.1:1", timeout=0.3))
            os.environ["TPS_PROXY_PROBE"] = "0"
            self.assertTrue(proxy_tcp_open("http://10.0.0.9:1", timeout=0.2))
        finally:
            os.environ["TPS_PROXY_PROBE"] = "0"

    def test_claim_gap_adaptive(self):
        import os
        os.environ.pop("TPS_CLAIM_GAP_SEC", None)
        r = make_redis()
        worker = LeaseWorker(r, 1, 1000, 8.0)
        worker._recent = [1] * 100
        self.assertGreater(worker._claim_gap_sec(), 10)
        worker._recent = []
        self.assertLess(worker._claim_gap_sec(), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
