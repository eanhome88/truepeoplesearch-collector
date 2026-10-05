#!/usr/bin/env python3
"""AccountLanes 单测：手写 Fake 时钟，不依赖任何第三方。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from account_lanes import AccountLanes  # noqa: E402


class FakeClock:
    """手写 Fake 时间源：可手动推进，替代 time.monotonic。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


ACCT_A_1 = "http://userA-region-US:secret@gate-a.example:8000"
# 同一网关账号的另一出口：仅 sid 粘性后缀不同，归一化后同桶（穿云多出口共享配额的真实形态）
ACCT_A_2 = "http://userA-region-US-sid-ab12cd34-t-30:secret@gate-a.example:8000"
ACCT_B_1 = "http://userB:secret@gate-b.example:8000"


class TestAccountIsolation(unittest.TestCase):
    def test_two_accounts_get_independent_buckets(self):
        clock = FakeClock()
        lanes = AccountLanes([ACCT_A_1, ACCT_B_1], qps_per_account=10.0,
                             fuse_sec=40.0, now=clock)
        self.assertEqual(lanes.checkout("g0"), ACCT_A_1)
        self.assertEqual(lanes.checkout("g1"), ACCT_B_1)
        # 双账号互相隔离：不同账号的 lane 不串桶
        self.assertEqual(lanes.lane_account(ACCT_A_1), lanes.lane_account(ACCT_A_2))
        self.assertNotEqual(lanes.lane_account(ACCT_A_1), lanes.lane_account(ACCT_B_1))

    def test_fuse_one_account_other_account_still_serves(self):
        clock = FakeClock()
        lanes = AccountLanes([ACCT_A_1, ACCT_A_2, ACCT_B_1], qps_per_account=10.0,
                             fuse_sec=40.0, now=clock)
        lanes.checkout("g0")  # 占住 A1
        fused = lanes.report_429(ACCT_A_1)
        self.assertEqual(fused, lanes.lane_account(ACCT_A_2))
        # 同账号另一出口也被跳过，只能拿到 B 账号的 lane
        self.assertEqual(lanes.checkout("g1"), ACCT_B_1)
        self.assertTrue(lanes.is_fused(lanes.lane_account(ACCT_A_1)))
        self.assertFalse(lanes.is_fused(lanes.lane_account(ACCT_B_1)))

    def test_fuse_expires_and_lane_recovers(self):
        clock = FakeClock()
        lanes = AccountLanes([ACCT_A_1, ACCT_B_1], qps_per_account=10.0,
                             fuse_sec=40.0, now=clock)
        lanes.checkout("g0")
        lanes.checkout("g1")
        lanes.report_429(ACCT_A_1)
        self.assertIsNone(lanes.checkout("g2"))  # A 熔断、B 被占用 -> 无可用
        clock.advance(41.0)
        self.assertEqual(lanes.checkout("g2"), ACCT_A_1)  # 熔断到期自动恢复

    def test_qps_limit_is_per_account(self):
        clock = FakeClock()
        lanes = AccountLanes([ACCT_A_1, ACCT_B_1], qps_per_account=1.0,
                             fuse_sec=40.0, now=clock)
        lanes.checkout("g0")  # 耗尽 A 账号 1s 窗口配额
        # A 账号无空闲 lane 可给 g1（A1 被占），但 B 账号配额独立、仍可服务
        self.assertEqual(lanes.checkout("g1"), ACCT_B_1)
        st = lanes.account_state(lanes.lane_account(ACCT_B_1))
        self.assertEqual(st["takes_last_sec"], 1)

    def test_second_account_is_pluggable(self):
        clock = FakeClock()
        lanes = AccountLanes([ACCT_A_1], qps_per_account=10.0,
                             fuse_sec=40.0, now=clock)
        self.assertEqual(lanes.checkout("g0"), ACCT_A_1)
        added = lanes.merge_urls([ACCT_A_1, ACCT_B_1])  # 重复的不计入
        self.assertEqual(added, 1)
        self.assertEqual(lanes.checkout("g1"), ACCT_B_1)
        self.assertEqual(lanes.holder_url("g0"), ACCT_A_1)  # 旧绑定不受影响


if __name__ == "__main__":
    unittest.main(verbosity=2)
