#!/usr/bin/env python3
"""粘性 IP：一条用到 429，再换一条休息好的。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_pool import StickyLanes, sticky_gateway_url, sticky_lanes_from_config  # noqa: E402
from test_tps_queue import make_redis  # noqa: E402


class TestStickyLanes(unittest.TestCase):
    def test_one_holder_keeps_the_same_ip(self):
        lanes = StickyLanes(["http://10.0.0.1:8000", "http://10.0.0.2:8000"], rest_sec=100)
        self.assertEqual(lanes.checkout("0"), "http://10.0.0.1:8000")
        self.assertEqual(lanes.checkout("0"), "http://10.0.0.1:8000")

    def test_two_holders_get_different_ips(self):
        lanes = StickyLanes(["http://10.0.0.1:8000", "http://10.0.0.2:8000"], rest_sec=100)
        self.assertEqual(lanes.checkout("0"), "http://10.0.0.1:8000")
        self.assertEqual(lanes.checkout("1"), "http://10.0.0.2:8000")
        self.assertIsNone(lanes.cool("0"))
        self.assertEqual(lanes.holder_url("0"), "http://10.0.0.1:8000")
        self.assertGreater(lanes.holder_rest("0"), 90)

    def test_429_switches_to_a_rested_ip_and_does_not_reuse_early(self):
        lanes = StickyLanes(["http://10.0.0.1:8000", "http://10.0.0.2:8000"], rest_sec=100)
        lanes.checkout("0")
        self.assertEqual(lanes.cool("0"), "http://10.0.0.2:8000")
        self.assertIsNone(lanes.cool("0"))
        self.assertEqual(lanes.holder_url("0"), "http://10.0.0.2:8000")
        lanes._rest_until["http://10.0.0.1:8000"] = 0
        self.assertEqual(lanes.cool("0"), "http://10.0.0.1:8000")

    def test_region_gateway_keeps_one_sid(self):
        url = sticky_gateway_url("http://acct-region-US:secret@gate.example:5000", "lane0", 120)
        self.assertIn("acct-region-US-sid-lane0-t-120", url)
        self.assertIn("@gate.example:5000", url)
        again = sticky_gateway_url(url, "other", 30)
        self.assertEqual(again, url)

    def test_surname_pages_move_ahead_of_letter_indexes(self):
        from discover import DISCOVER_PENDING, prioritize_surname_pages

        r = make_redis()
        r.rpush(
            DISCOVER_PENDING,
            "https://www.truepeoplesearch.com/find/b",
            "https://www.truepeoplesearch.com/find/smith",
            "https://www.truepeoplesearch.com/find/c",
        )
        self.assertEqual(prioritize_surname_pages(r), 1)
        self.assertEqual(
            r.lrange(DISCOVER_PENDING, 0, -1)[0],
            "https://www.truepeoplesearch.com/find/smith",
        )

    def test_tunnel_config_is_not_a_sticky_lane(self):
        self.assertIsNone(sticky_lanes_from_config({
            "mode": "tunnel",
            "tunnel": "http://user:secret@gate.example:8000",
        }))


if __name__ == "__main__":
    unittest.main(verbosity=2)
