#!/usr/bin/env python3
"""tps_queue 契约测试。可在 scripts/ 下执行：python3 test_tps_queue.py"""

from __future__ import annotations

import json
import sys
import time
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


from tps_queue import (  # noqa: E402
    LEASE_SEC,
    MAX_ATTEMPTS,
    QUEUED_KEY,
    SEEN_KEY,
    ack,
    claim,
    extract_person_id,
    feed,
    nack,
    release,
    peek_processing,
    queue_stats,
    recover_expired,
)


PID = "px82l44nur68u2l2l8n60"
URL = f"https://www.truepeoplesearch.com/find/person/{PID}"
URL_SAME_PERSON = f"https://www.truepeoplesearch.com/find/person/{PID}?src=dup"
URL_OTHER = "https://www.truepeoplesearch.com/find/person/abc123def456"


class MemoryRedis:
    """浅层内存 Redis：LIST / SET / ZSET / STRING / pipeline / BLMOVE。"""

    def __init__(self):
        self._kv = {}
        self._lists = {}
        self._sets = {}
        self._zsets = {}
        self._hashes = {}
        self._expire_at = {}

    def _alive(self, key):
        exp = self._expire_at.get(key)
        if exp is not None and exp <= time.time():
            self.delete(key)
            return False
        return True

    def _str(self, value):
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def pipeline(self, transaction=True, shard_hint=None):
        return _MemoryPipeline(self)

    def get(self, name):
        name = self._str(name)
        if not self._alive(name):
            return None
        return self._kv.get(name)

    def set(self, name, value, ex=None, px=None, nx=False, xx=False, **kwargs):
        name = self._str(name)
        exists = self._alive(name) and name in self._kv
        if nx and exists:
            return None
        if xx and not exists:
            return None
        self._kv[name] = self._str(value)
        if ex is not None:
            self._expire_at[name] = time.time() + float(ex)
        elif px is not None:
            self._expire_at[name] = time.time() + float(px) / 1000.0
        else:
            self._expire_at.pop(name, None)
        return True

    def setex(self, name, time_, value):
        return self.set(name, value, ex=time_)

    def delete(self, *names):
        n = 0
        for name in names:
            name = self._str(name)
            gone = False
            for store in (self._kv, self._lists, self._sets, self._zsets, self._hashes):
                if name in store:
                    del store[name]
                    gone = True
            if name in self._expire_at:
                del self._expire_at[name]
                gone = True
            n += int(gone)
        return n

    def exists(self, *names):
        c = 0
        for name in names:
            name = self._str(name)
            if not self._alive(name):
                continue
            if name in self._kv or name in self._lists or name in self._sets or name in self._zsets or name in self._hashes:
                c += 1
        return c

    def expire(self, name, seconds):
        name = self._str(name)
        if not self.exists(name):
            return False
        self._expire_at[name] = time.time() + float(seconds)
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

    def _list(self, name):
        name = self._str(name)
        self._alive(name)
        return self._lists.setdefault(name, [])

    def lpush(self, name, *values):
        lst = self._list(name)
        for v in values:
            lst.insert(0, self._str(v))
        return len(lst)

    def rpush(self, name, *values):
        lst = self._list(name)
        for v in values:
            lst.append(self._str(v))
        return len(lst)

    def lpop(self, name, count=None):
        lst = self._list(name)
        if not lst:
            return None
        if count is None:
            return lst.pop(0)
        out = []
        for _ in range(int(count)):
            if not lst:
                break
            out.append(lst.pop(0))
        return out

    def rpop(self, name):
        lst = self._list(name)
        if not lst:
            return None
        return lst.pop()

    def llen(self, name):
        return len(self._list(name))

    def lrange(self, name, start, end):
        lst = self._list(name)
        if end == -1:
            end = len(lst) - 1
        return lst[start : end + 1]

    def lindex(self, name, index):
        lst = self._list(name)
        try:
            return lst[int(index)]
        except (IndexError, ValueError):
            return None

    def lrem(self, name, count, value):
        lst = self._list(name)
        value = self._str(value)
        removed = 0
        if count == 0:
            new = [x for x in lst if x != value]
            removed = len(lst) - len(new)
            lst[:] = new
            return removed
        step = 1 if count > 0 else -1
        idxs = range(len(lst)) if count > 0 else range(len(lst) - 1, -1, -1)
        drop = set()
        need = abs(int(count))
        for i in idxs:
            if lst[i] == value:
                drop.add(i)
                removed += 1
                if removed >= need:
                    break
        lst[:] = [x for i, x in enumerate(lst) if i not in drop]
        return removed

    def blmove(self, first_list, second_list, timeout=0, src="LEFT", dest="RIGHT"):
        src = self._str(src).upper()
        dest = self._str(dest).upper()
        val = self.lpop(first_list) if src == "LEFT" else self.rpop(first_list)
        if val is None:
            return None
        if dest == "LEFT":
            self.lpush(second_list, val)
        else:
            self.rpush(second_list, val)
        return val

    def lmove(self, first_list, second_list, src="LEFT", dest="RIGHT"):
        return self.blmove(first_list, second_list, 0, src, dest)

    def sadd(self, name, *values):
        name = self._str(name)
        self._alive(name)
        s = self._sets.setdefault(name, set())
        n = 0
        for v in values:
            v = self._str(v)
            if v not in s:
                s.add(v)
                n += 1
        return n

    def sismember(self, name, value):
        name = self._str(name)
        if not self._alive(name):
            return False
        return self._str(value) in self._sets.get(name, set())

    def scard(self, name):
        name = self._str(name)
        if not self._alive(name):
            return 0
        return len(self._sets.get(name, set()))

    def srem(self, name, *values):
        name = self._str(name)
        s = self._sets.get(name, set())
        n = 0
        for v in values:
            v = self._str(v)
            if v in s:
                s.discard(v)
                n += 1
        return n

    def sscan(self, name, cursor=0, match=None, count=None):
        members = list(self._sets.get(self._str(name), set()))
        return (0, members)

    def hset(self, name, key=None, value=None, mapping=None):
        name = self._str(name)
        self._alive(name)
        h = self._hashes.setdefault(name, {})
        n = 0
        if mapping:
            for k, v in mapping.items():
                k = self._str(k)
                if k not in h:
                    n += 1
                h[k] = self._str(v)
        if key is not None:
            k = self._str(key)
            if k not in h:
                n += 1
            h[k] = self._str(value)
        return n

    def hget(self, name, key):
        name = self._str(name)
        if not self._alive(name):
            return None
        return self._hashes.get(name, {}).get(self._str(key))

    def hgetall(self, name):
        name = self._str(name)
        if not self._alive(name):
            return {}
        return dict(self._hashes.get(name, {}))

    def smembers(self, name):
        name = self._str(name)
        if not self._alive(name):
            return set()
        return set(self._sets.get(name, set()))

    def zadd(self, name, mapping=None, nx=False, xx=False, ch=False, incr=False, **kwargs):
        name = self._str(name)
        self._alive(name)
        if mapping is None:
            mapping = kwargs
        z = self._zsets.setdefault(name, {})
        added = 0
        for member, score in mapping.items():
            member = self._str(member)
            score = float(score)
            exists = member in z
            if nx and exists:
                continue
            if xx and not exists:
                continue
            if incr and exists:
                z[member] = z[member] + score
            else:
                if not exists:
                    added += 1
                z[member] = score
        return added

    def zrem(self, name, *members):
        name = self._str(name)
        z = self._zsets.get(name, {})
        n = 0
        for m in members:
            if z.pop(self._str(m), None) is not None:
                n += 1
        return n

    def zscore(self, name, member):
        name = self._str(name)
        z = self._zsets.get(name, {})
        score = z.get(self._str(member))
        return None if score is None else float(score)

    def zcard(self, name):
        return len(self._zsets.get(self._str(name), {}))

    def zrange(self, name, start, end, withscores=False, **kwargs):
        items = sorted(self._zsets.get(self._str(name), {}).items(), key=lambda kv: (kv[1], kv[0]))
        if end == -1:
            end = len(items) - 1
        sl = items[start : end + 1]
        if withscores:
            return [(m, s) for m, s in sl]
        return [m for m, _ in sl]

    def _zbound(self, bound, lo):
        if bound in ("-inf", b"-inf"):
            return float("-inf")
        if bound in ("+inf", b"+inf"):
            return float("inf")
        return float(bound)

    def zrangebyscore(self, name, min, max, start=None, num=None, withscores=False, **kwargs):
        lo, hi = self._zbound(min, True), self._zbound(max, False)
        items = [
            (m, s)
            for m, s in sorted(
                self._zsets.get(self._str(name), {}).items(), key=lambda kv: (kv[1], kv[0])
            )
            if lo <= s <= hi
        ]
        if start is not None and num is not None:
            items = items[int(start) : int(start) + int(num)]
        if withscores:
            return items
        return [m for m, _ in items]

    def zcount(self, name, min, max):
        return len(self.zrangebyscore(name, min, max))


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


def _force_expire_leases(r, score=1.0):
    r.delete("tps:recover_lock")
    members = r.zrange("tps:leases", 0, -1)
    if members:
        r.zadd("tps:leases", {m: float(score) for m in members})
    for m in members or []:
        mid = m.decode("utf-8") if isinstance(m, bytes) else m
        raw = r.get(f"tps:job:{mid}")
        if not raw:
            continue
        data = json.loads(raw)
        data["lease_until"] = score
        payload = json.dumps(data)
        r.set(f"tps:job:{mid}", payload)


class TestExtractPersonId(unittest.TestCase):
    def test_extract_person_id(self):
        self.assertEqual(extract_person_id(URL), PID)
        self.assertEqual(extract_person_id(URL_SAME_PERSON), PID)
        self.assertEqual(
            extract_person_id("/find/person/abc123def456"),
            "abc123def456",
        )


class TestTpsQueue(unittest.TestCase):
    def setUp(self):
        self.r = make_redis()
        self.assertEqual(MAX_ATTEMPTS, 3)
        self.assertEqual(LEASE_SEC, 90)

    def test_feed_dedup_same_person_id(self):
        result = feed(self.r, [URL, URL_SAME_PERSON])
        stats = queue_stats(self.r)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(self.r.scard(SEEN_KEY), 0)
        self.assertEqual(self.r.scard(QUEUED_KEY), 1)
        if isinstance(result, dict):
            self.assertEqual(result.get("enqueued"), 1)
            self.assertEqual(result.get("deduped"), 1)
        again = feed(self.r, [URL])
        stats = queue_stats(self.r)
        self.assertEqual(stats["pending"], 1)
        if isinstance(again, dict):
            self.assertEqual(again.get("enqueued"), 0)
            self.assertGreaterEqual(again.get("deduped", 0), 1)

    def test_ack_marks_seen_not_feed(self):
        feed(self.r, [URL])
        self.assertFalse(self.r.sismember(SEEN_KEY, PID))
        job = claim(self.r, "worker-seen")
        ack(self.r, job)
        self.assertTrue(self.r.sismember(SEEN_KEY, PID))
        self.assertFalse(self.r.sismember(QUEUED_KEY, PID))
        third = feed(self.r, [URL])
        self.assertEqual(third.get("enqueued"), 0)
        self.assertGreaterEqual(third.get("deduped", 0), 1)

    def test_claim_ack_pending_processing_counts(self):
        feed(self.r, [URL, URL_OTHER])
        before = queue_stats(self.r)
        self.assertEqual(before["pending"], 2)
        self.assertEqual(before["processing"], 0)

        job = claim(self.r, "worker-1")
        self.assertIsNotNone(job)
        self.assertEqual(job["person_id"], extract_person_id(job["url"]))
        after_claim = queue_stats(self.r)
        self.assertEqual(after_claim["pending"], before["pending"] - 1)
        self.assertEqual(after_claim["processing"], before["processing"] + 1)

        ack(self.r, job)
        after_ack = queue_stats(self.r)
        self.assertEqual(after_ack["processing"], after_claim["processing"] - 1)
        self.assertEqual(after_ack["pending"], after_claim["pending"])
        self.assertEqual(after_ack.get("dlq", 0), 0)

    def test_release_returns_pending_without_attempt_or_dlq(self):
        feed(self.r, [URL])
        job = claim(self.r, "worker-release")
        self.assertIsNotNone(job)
        self.assertEqual(int(job.get("attempts") or 0), 0)
        release(self.r, job, "HTTP 429")

        stats = queue_stats(self.r)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats.get("dlq", 0), 0)

        again = claim(self.r, "worker-release-2")
        self.assertIsNotNone(again)
        self.assertEqual(again["person_id"], PID)
        self.assertEqual(int(again.get("attempts") or 0), 0)
        self.assertEqual(again.get("last_error"), "HTTP 429")

    def test_nack_exceeds_max_attempts_goes_to_dlq(self):
        feed(self.r, [URL])
        for i in range(MAX_ATTEMPTS + 1):
            stats = queue_stats(self.r)
            if stats.get("dlq", 0) >= 1:
                break
            if stats.get("pending", 0) < 1 and stats.get("processing", 0) < 1:
                break
            job = claim(self.r, f"worker-nack-{i}")
            self.assertIsNotNone(job, f"claim 在第 {i} 轮返回空，此时 stats={stats}")
            nack(self.r, job, f"simulated failure {i}")

        stats = queue_stats(self.r)
        self.assertGreaterEqual(stats["dlq"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["processing"], 0)
        self.assertGreaterEqual(self.r.llen("tps:dlq"), 1)

    def test_peek_processing_lists_claimed_job(self):
        feed(self.r, [URL])
        job = claim(self.r, "worker-peek")
        self.assertIsNotNone(job)
        rows = peek_processing(self.r, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person_id"], PID)
        self.assertIn("/person/", rows[0]["url"])

    def test_recover_expired_moves_lease_back(self):
        feed(self.r, [URL])
        job = claim(self.r, "worker-lease")
        self.assertIsNotNone(job)
        claimed = queue_stats(self.r)
        self.assertEqual(claimed["pending"], 0)
        self.assertEqual(claimed["processing"], 1)

        _force_expire_leases(self.r, score=1.0)
        recovered = recover_expired(self.r)
        self.assertGreaterEqual(recovered, 1)

        stats = queue_stats(self.r)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["processing"], 0)
        self.assertEqual(stats.get("dlq", 0), 0)

        again = claim(self.r, "worker-lease-2")
        self.assertIsNotNone(again)
        self.assertEqual(again["person_id"], PID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
