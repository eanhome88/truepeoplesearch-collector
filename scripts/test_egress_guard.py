#!/usr/bin/env python3
"""egress_guard 单测：手写 Fake 探针，无真实网络，不碰 cf_farm。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egress_guard import (  # noqa: E402
    EgressGuard,
    drop_box_session_if_egress_moved,
    expire_own_cf_session_if_egress_moved,
)


class FakeProbe:
    """手写 Fake：按脚本返回出口 IP；"RAISE" 表示探测失败（超时）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, proxy_url, timeout):
        self.calls.append((proxy_url, timeout))
        if not self.script:
            return ""
        nxt = self.script.pop(0)
        if nxt == "RAISE":
            raise TimeoutError("fake probe timeout")
        return nxt


class FakeOwnCfSession:
    def __init__(self):
        self.proxy = "http://user:pass@10.0.0.1:8000"
        self.expires_at = 9999999999.0
        self.resolves = 0

    @property
    def expired(self):
        import time
        return bool(self.expires_at) and time.time() >= self.expires_at

    def resolve(self):
        self.resolves += 1


class FakeBox:
    def __init__(self):
        self.proxy = "http://user:pass@10.0.0.1:8000"
        self.session = object()


class TestEgressGuard(unittest.TestCase):
    def test_same_egress_keeps_session(self):
        """出口不变：两次 check 都 False，会话不动。"""
        guard = EgressGuard(probe=FakeProbe(["1.2.3.4", "1.2.3.4"]))
        proxy = "http://user:pass@10.0.0.1:8000"
        self.assertFalse(guard.check_lane("0", proxy))
        self.assertFalse(guard.check_lane("0", proxy))
        self.assertEqual(guard.last_ip("0"), "1.2.3.4")

    def test_egress_move_expires_own_cf_session(self):
        """出口漂移：check True，且 helper 置 expires_at=0 触发重解。"""
        guard = EgressGuard(probe=FakeProbe(["1.2.3.4", "5.6.7.8"]))
        proxy = "http://user:pass@10.0.0.1:8000"
        self.assertFalse(guard.check_lane("0", proxy))
        sess = FakeOwnCfSession()
        moved = expire_own_cf_session_if_egress_moved("0", proxy, sess, guard)
        self.assertTrue(moved)
        import time as _t
        self.assertLessEqual(sess.expires_at, _t.time())
        self.assertTrue(sess.expired)

    def test_probe_failure_never_blocks(self):
        """探测抛错/回空：放行 False，会话与记录都不动。"""
        guard = EgressGuard(probe=FakeProbe(["1.2.3.4", "RAISE", ""]))
        proxy = "http://user:pass@10.0.0.1:8000"
        self.assertFalse(guard.check_lane("0", proxy))
        self.assertFalse(guard.check_lane("0", proxy))
        self.assertFalse(guard.check_lane("0", proxy))
        self.assertEqual(guard.last_ip("0"), "1.2.3.4")
        box = FakeBox()
        kept = box.session
        self.assertFalse(drop_box_session_if_egress_moved(box, "0", guard))
        self.assertIs(box.session, kept)


if __name__ == "__main__":
    unittest.main(verbosity=2)
