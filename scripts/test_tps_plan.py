#!/usr/bin/env python3
import unittest

from tps_plan import (
    DAILY_TARGET,
    PROTOCOL_SEC,
    SITE_UNIVERSE,
    assign_lane,
    build_plan,
    daily_pages,
    days_for,
    inflight_for_target,
    normalize_plan,
)


class TestAssignLane(unittest.TestCase):
    def test_same_surname_stays_on_one_lane(self):
        first = assign_lane("angelo", 42)
        self.assertEqual(first, assign_lane("Angelo", 42))
        self.assertGreaterEqual(first, 0)
        self.assertLess(first, 42)

    def test_single_lane_is_zero(self):
        self.assertEqual(assign_lane("angelo", 1), 0)
        self.assertEqual(assign_lane("angelo", 0), 0)


class TestProtocolPlan(unittest.TestCase):
    def test_three_million_at_measured_protocol_time_needs_42(self):
        self.assertEqual(PROTOCOL_SEC, 1.2)
        self.assertEqual(inflight_for_target(1.2), 42)
        self.assertEqual(daily_pages(42, 1.2), 3_024_000)
        self.assertGreaterEqual(daily_pages(42, 1.2), DAILY_TARGET)

    def test_universe_is_84_days_at_three_million(self):
        plan = build_plan(0, 1, 1.2, 501000, 24)
        self.assertEqual(plan["mode"], "protocol")
        self.assertEqual(plan["target_per_day"], 3_000_000)
        self.assertEqual(plan["per_day"], 3_000_000)
        self.assertEqual(plan["lanes_needed"], 42)
        self.assertEqual(plan["inflight_needed"], 42)
        self.assertEqual(plan["days_universe"], 84)
        self.assertEqual(plan["days_scope"], 1)
        self.assertEqual(plan["universe"], SITE_UNIVERSE)
        self.assertEqual(plan["shortfall"], 42)

    def test_two_requests_per_exit_halves_the_exit_count(self):
        plan = build_plan(21, 2, 1.2, 0, 0)
        self.assertEqual(plan["lanes_needed"], 21)
        self.assertEqual(plan["shortfall"], 0)
        self.assertEqual(plan["active_requests"], 42)

    def test_normalize_clamps_and_marks_protocol(self):
        cfg = normalize_plan(lanes=99999, inflight=99, page_sec=0)
        self.assertEqual(cfg["mode"], "protocol")
        self.assertEqual(cfg["lanes"], 2000)
        self.assertEqual(cfg["inflight"], 16)
        self.assertEqual(cfg["page_sec"], 0.2)

    def test_days_for_rounds_up(self):
        self.assertEqual(days_for(10, 3), 4)
        self.assertEqual(days_for(0, 3), 0)
        self.assertIsNone(days_for(10, 0))
        self.assertEqual(days_for(SITE_UNIVERSE, DAILY_TARGET), 84)


if __name__ == "__main__":
    unittest.main()
