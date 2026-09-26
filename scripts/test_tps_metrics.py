#!/usr/bin/env python3
"""tps_metrics 契约测试。可在 scripts/ 下执行：python3 test_tps_metrics.py"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import fakeredis

    def make_redis():
        return fakeredis.FakeRedis(decode_responses=True)

except ImportError:

    def make_redis():
        return MemoryRedis()


from tps_metrics import get_metrics  # noqa: E402


class MemoryRedis:
    """浅层内存 Redis：STRING + pipeline，足够 incr / snapshot / prometheus。"""

    def __init__(self):
        self._kv = {}

    def _str(self, value):
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def pipeline(self, transaction=True, shard_hint=None):
        return _MemoryPipeline(self)

    def get(self, name):
        return self._kv.get(self._str(name))

    def set(self, name, value, **kwargs):
        self._kv[self._str(name)] = self._str(value)
        return True

    def incr(self, name, amount=1):
        return self.incrby(name, amount)

    def incrby(self, name, amount=1):
        cur = self.get(name)
        nxt = (int(float(cur)) if cur is not None else 0) + int(amount)
        self.set(name, str(nxt))
        return nxt

    def incrbyfloat(self, name, amount=1.0):
        cur = self.get(name)
        nxt = (float(cur) if cur is not None else 0.0) + float(amount)
        self.set(name, str(nxt))
        return nxt

    def mget(self, keys, *args):
        if args:
            all_keys = (keys,) + args
        elif isinstance(keys, (list, tuple)):
            all_keys = keys
        else:
            all_keys = (keys,)
        return [self.get(k) for k in all_keys]


class _MemoryPipeline:
    def __init__(self, client):
        self._client = client
        self._cmds = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cmds.clear()

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self._cmds.append((name, args, kwargs))
            return self

        return _call

    def execute(self, raise_on_error=True):
        out = []
        for name, args, kwargs in self._cmds:
            out.append(getattr(self._client, name)(*args, **kwargs))
        self._cmds.clear()
        return out


class TestTpsMetrics(unittest.TestCase):
    def setUp(self):
        self.r = make_redis()
        self.m = get_metrics(self.r)

    def test_incr_and_prometheus_contains_tps_jobs_total(self):
        self.m.incr("success")
        self.m.incr("success")
        self.m.incr("dlq", 1)

        snap = self.m.snapshot()
        self.assertEqual(snap["counters"]["success"], 2)
        self.assertEqual(snap["counters"]["dlq"], 1)

        text = self.m.render_prometheus()
        self.assertIn("tps_jobs_total", text)
        self.assertRegex(text, r'tps_jobs_total\{bucket="success"\}\s+2')
        self.assertRegex(text, r'tps_jobs_total\{bucket="dlq"\}\s+1')


if __name__ == "__main__":
    unittest.main(verbosity=2)
