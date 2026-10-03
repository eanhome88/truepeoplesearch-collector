"""Atomic claim failure/interleaving regressions; fakeredis only, no live services."""

import json
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import fakeredis
from redis.exceptions import ConnectionError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tps_queue as queue


def url(person_id):
    return f"https://example.invalid/find/person/{person_id}"


class AtomicClaimTests(unittest.TestCase):
    def setUp(self):
        self.server = fakeredis.FakeServer()
        self.r = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        self.other = fakeredis.FakeRedis(server=self.server, decode_responses=True)

    def inject_before_first_exec(self, action):
        """A second connection or crash runs after WATCH/read, before EXEC."""
        factory = self.r.pipeline
        fired = False

        def pipeline(*args, **kwargs):
            pipe = factory(*args, **kwargs)
            execute = pipe.execute

            def intercepted(*exec_args, **exec_kwargs):
                nonlocal fired
                if not fired:
                    fired = True
                    action()
                return execute(*exec_args, **exec_kwargs)

            pipe.execute = intercepted
            return pipe

        return mock.patch.object(self.r, "pipeline", side_effect=pipeline)

    def assert_owned(self, job):
        jid = job["id"]
        self.assertIn(jid, self.r.lrange(queue.PROCESSING_KEY, 0, -1))
        self.assertEqual(self.r.zscore(queue.LEASES_KEY, jid), job["lease_until"])
        self.assertEqual(json.loads(self.r.get(queue._job_key(jid))), job)

    def test_fifo_and_binary_responses(self):
        queue.feed(self.r, [url("first"), url("second"), url("third")])
        binary = fakeredis.FakeRedis(server=self.server, decode_responses=False)
        jobs = [queue.claim(binary, b"binary-worker") for _ in range(3)]
        self.assertEqual([job["person_id"] for job in jobs], ["first", "second", "third"])
        for job in jobs:
            self.assertEqual(job["worker_id"], "binary-worker")
            self.assert_owned(job)

    def test_crash_before_exec_leaves_original_pending_job(self):
        queue.feed(self.r, [url("beforeCrash")])
        jid = self.r.lindex(queue.PENDING_KEY, -1)
        original = self.r.get(queue._job_key(jid))

        def crash():
            raise ConnectionError("simulated disconnect before EXEC")

        with self.inject_before_first_exec(crash):
            with self.assertRaises(ConnectionError):
                queue.claim(self.r, "interrupted")

        self.assertEqual(self.r.lrange(queue.PENDING_KEY, 0, -1), [jid])
        self.assertEqual(self.r.llen(queue.PROCESSING_KEY), 0)
        self.assertIsNone(self.r.zscore(queue.LEASES_KEY, jid))
        self.assertEqual(self.r.get(queue._job_key(jid)), original)
        self.assert_owned(queue.claim(self.r, "replacement"))

    def test_lost_exec_response_always_leaves_recoverable_lease(self):
        queue.feed(self.r, [url("afterCrash")])
        jid = self.r.lindex(queue.PENDING_KEY, -1)
        factory = self.r.pipeline

        def pipeline(*args, **kwargs):
            pipe = factory(*args, **kwargs)
            execute = pipe.execute

            def lost_response(*exec_args, **exec_kwargs):
                execute(*exec_args, **exec_kwargs)
                raise ConnectionError("simulated lost EXEC response")

            pipe.execute = lost_response
            return pipe

        with mock.patch.object(self.r, "pipeline", side_effect=pipeline):
            with self.assertRaises(ConnectionError):
                queue.claim(self.r, "interrupted")

        self.assertEqual(self.r.llen(queue.PENDING_KEY), 0)
        job = json.loads(self.r.get(queue._job_key(jid)))
        self.assert_owned(job)
        self.assertEqual(job["worker_id"], "interrupted")
        self.r.zadd(queue.LEASES_KEY, {jid: 1.0})
        self.assertEqual(queue.recover_expired(self.r), 1)
        self.assertEqual(queue.recover_expired(self.r), 0)
        self.assertEqual(self.r.lrange(queue.PENDING_KEY, 0, -1), [jid])
        self.assertEqual(self.r.llen(queue.PROCESSING_KEY), 0)
        replacement = queue.claim(self.r, "replacement")
        self.assertEqual(replacement["id"], jid)
        self.assertEqual(replacement["attempts"], 1)
        self.assert_owned(replacement)

    def test_interleaved_workers_claim_distinct_jobs_in_fifo_order(self):
        queue.feed(self.r, [url("first"), url("second")])
        competing = []
        with self.inject_before_first_exec(
            lambda: competing.append(queue.claim(self.other, "other-worker"))
        ):
            job = queue.claim(self.r, "original-worker")

        self.assertEqual(competing[0]["person_id"], "first")
        self.assertEqual(job["person_id"], "second")
        self.assertNotEqual(competing[0]["id"], job["id"])
        self.assertEqual(self.r.llen(queue.PROCESSING_KEY), 2)
        self.assertEqual(self.r.zcard(queue.LEASES_KEY), 2)
        self.assert_owned(competing[0])
        self.assert_owned(job)

    def test_interleaved_enqueue_keeps_oldest_first(self):
        queue.feed(self.r, [url("oldest")])
        with self.inject_before_first_exec(lambda: queue.feed(self.other, [url("newest")])):
            job = queue.claim(self.r, "worker")
        self.assertEqual(job["person_id"], "oldest")
        self.assertEqual(queue.claim(self.r, "worker")["person_id"], "newest")

    def test_payload_change_during_claim_is_reread_before_commit(self):
        queue.feed(self.r, [url("changed")])
        jid = self.r.lindex(queue.PENDING_KEY, -1)

        def update_payload():
            job = json.loads(self.other.get(queue._job_key(jid)))
            job["attempts"] = 2
            job["last_error"] = "captcha hold"
            self.other.set(queue._job_key(jid), queue._dumps(job))

        with self.inject_before_first_exec(update_payload):
            job = queue.claim(self.r, "worker")
        self.assertEqual(job["attempts"], 2)
        self.assertEqual(job["last_error"], "captcha hold")
        self.assert_owned(job)
        self.r.zadd(queue.LEASES_KEY, {jid: 1.0})
        self.assertEqual(queue.recover_expired(self.r), 1)
        resumed = queue.claim(self.r, "resumed")
        self.assertEqual(resumed["attempts"], 2)
        self.assertEqual(resumed["last_error"], "captcha hold")
        self.assertEqual(self.r.llen(queue.DLQ_KEY), 0)

    def test_missing_or_invalid_payload_does_not_create_processing_orphan(self):
        for payload in (None, "not JSON", "[]"):
            with self.subTest(payload=payload):
                self.r.flushall()
                self.r.lpush(queue.PENDING_KEY, "invalid")
                if payload is not None:
                    self.r.set(queue._job_key("invalid"), payload)
                self.assertIsNone(queue.claim(self.r, "worker"))
                self.assertEqual(self.r.llen(queue.PENDING_KEY), 0)
                self.assertEqual(self.r.llen(queue.PROCESSING_KEY), 0)
                self.assertEqual(self.r.zcard(queue.LEASES_KEY), 0)

    def test_empty_poll_notices_new_work(self):
        with mock.patch.object(queue.time, "sleep", side_effect=lambda _: queue.feed(
            self.other, [url("arrived")]
        )) as sleep:
            job = queue.claim(self.r, "worker")
        self.assertEqual(job["person_id"], "arrived")
        sleep.assert_called_once()
        self.assert_owned(job)

    def test_empty_queue_timeout_is_bounded(self):
        started = time.monotonic()
        with mock.patch.object(queue, "CLAIM_TIMEOUT_SEC", 0.03):
            self.assertIsNone(queue.claim(self.r, "worker"))
        self.assertGreaterEqual(time.monotonic() - started, 0.03)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_parallel_claims_never_duplicate_or_leave_unleased_processing(self):
        queue.feed(self.r, [url(f"person{i}") for i in range(48)])

        def claim_six(worker):
            client = fakeredis.FakeRedis(server=self.server, decode_responses=True)
            return [queue.claim(client, worker) for _ in range(6)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(claim_six, [f"worker{i}" for i in range(8)]))
        jobs = [job for batch in batches for job in batch]
        self.assertTrue(all(job is not None for job in jobs))
        self.assertEqual(len({job["id"] for job in jobs}), 48)
        processing = self.r.lrange(queue.PROCESSING_KEY, 0, -1)
        self.assertEqual(len(processing), 48)
        self.assertEqual(set(processing), set(self.r.zrange(queue.LEASES_KEY, 0, -1)))
        self.assertEqual(self.r.llen(queue.PENDING_KEY), 0)
        for job in jobs:
            self.assert_owned(job)


if __name__ == "__main__":
    unittest.main()
