"""入库真实性与可靠缓冲的离线回归；不访问网站或业务数据库。"""

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fakeredis

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import batch_ingest
import bulk_ingester_daemon
import multi_worker_runner
import protocol_fetcher
import protocol_worker
import scrape_to_tidb
import tps_control
import tps_queue
from tools import test_live_scrape


def person(with_phone=True):
    value = {
        "person_id": "person-test",
        "full_name": "Test Person",
        "current_address_text": "Example address",
        "current_address": {"city": "Example"},
    }
    if with_phone:
        value["primary_phone"] = "(202) 555-0100"
        value["primary_phone_type"] = "Wireless"
        value["phone_numbers"] = [{"phone_number": "(202) 555-0100", "line_type": "Wireless"}]
    return value


class InsertPersonTruthTests(unittest.TestCase):
    def test_business_phone_outside_person_section_is_not_attributed(self):
        text = """Test Person
Businesses
Example Shop
(202) 555-0100 - Wireless
"""
        self.assertEqual(scrape_to_tidb.extract_phone_numbers(text), [])

    def test_person_phone_section_stops_before_business_and_unknown_type_is_not_wireless(self):
        text = """Test Person
Phone Numbers
(202) 555-0100
Last reported Jan 2025
Businesses
Example Shop
(303) 555-0101 - Wireless
"""
        phones = scrape_to_tidb.extract_phone_numbers(text)
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["phone_number"], "(202) 555-0100")
        self.assertIsNone(phones[0]["line_type"])

    def test_carrier_name_wireless_does_not_create_a_wireless_line_type(self):
        phones = scrape_to_tidb.extract_phone_numbers("""Phone Numbers
(202) 555-0100
Last reported Jan 2025
Verizon Wireless
Email Addresses
person@example.invalid
""")
        self.assertEqual(len(phones), 1)
        self.assertIsNone(phones[0]["line_type"])
        self.assertFalse(scrape_to_tidb.has_usable_phone({"phone_numbers": phones}))

    def test_phone_quality_accepts_landline_or_mobile_but_not_empty_placeholders(self):
        landline = {"phone_numbers": [{"phone_number": "(202) 555-0100", "line_type": "Landline"}]}
        mobile = {"primary_phone": "(303) 555-0101", "primary_phone_type": "Wireless"}
        self.assertTrue(scrape_to_tidb.has_usable_phone(landline))
        self.assertTrue(scrape_to_tidb.has_usable_phone(mobile))
        self.assertTrue(scrape_to_tidb.has_usable_phone({"primary_phone": "(303) 555-0101", "primary_phone_type": "Landline/Services"}))
        self.assertTrue(scrape_to_tidb.has_usable_phone({"wireless_phone_1": "(303) 555-0101"}))
        for phone_type in (None, "", "Unknown", "Voip", "VoIP", "Business"):
            with self.subTest(phone_type=phone_type):
                self.assertFalse(scrape_to_tidb.has_usable_phone({
                    "primary_phone": "(202) 555-0100", "primary_phone_type": phone_type,
                    "phone_numbers": [{"phone_number": "(303) 555-0101", "line_type": phone_type}],
                }))
        for value in (None, "", "N/A", "***-***-****", "0000000000", "1111111111"):
            with self.subTest(value=value):
                self.assertFalse(scrape_to_tidb.has_usable_phone({"primary_phone": value}))
        for value in ("foo (212) 555-1234", "(212) 555-1234 ext 1", "(212) 555-1234\nWireless"):
            with self.subTest(value=value):
                self.assertFalse(scrape_to_tidb.has_usable_phone({
                    "primary_phone": value, "primary_phone_type": "Wireless",
                }))

    def test_mixed_phone_types_never_promote_voip_or_unknown_to_primary(self):
        page = SimpleNamespace(get_all_text=lambda: """Mixed Person, Age 42
Phone Numbers
(202) 555-0100 - VoIP - Possible Primary
(303) 555-0101
(212) 555-0102 - Landline
Businesses
(404) 555-0103 - Wireless
""", css=lambda *_: SimpleNamespace(get=lambda: "Mixed Person, Age 42"))
        with mock.patch.object(scrape_to_tidb, "extract_person_id", return_value="person-test"):
            data = scrape_to_tidb.parse_person(page, "https://example.invalid/find/person/person-test")
        self.assertEqual(data["primary_phone"], "(212) 555-0102")
        self.assertEqual(data["primary_phone_type"], "Landline")
        self.assertTrue(scrape_to_tidb.has_usable_phone(data))
        lean = protocol_fetcher.parse_person_lean(page, "https://example.invalid/find/person/person-test")
        self.assertEqual(lean["primary_phone"], "(212) 555-0102")
        self.assertEqual(lean["primary_phone_type"], "Landline")

    def test_no_phone_or_empty_phone_object_never_opens_db_cursor(self):
        db = mock.Mock()
        self.assertFalse(scrape_to_tidb.insert_person(db, person(False)))
        empty = person(False)
        empty["phone_numbers"] = [{"phone_number": ""}]
        self.assertFalse(scrape_to_tidb.insert_person(db, empty))
        db.cursor.assert_not_called()

    def test_committed_write_returns_true(self):
        db = mock.Mock()
        cursor = db.cursor.return_value
        with mock.patch.object(scrape_to_tidb, "_persons_columns", return_value=set()), \
             mock.patch.object(scrape_to_tidb, "_upsert_person"), \
             mock.patch.object(scrape_to_tidb, "_upsert_children"), \
             mock.patch.object(scrape_to_tidb, "_try_update_counts"):
            self.assertTrue(scrape_to_tidb.insert_person(db, person()))
        db.commit.assert_called_once()
        cursor.close.assert_called_once()

    def test_unchanged_existing_row_returns_true_because_it_is_persisted(self):
        db = mock.Mock()
        cursor = db.cursor.return_value
        cursor.fetchone.return_value = ("same-hash",)
        with mock.patch.object(scrape_to_tidb, "_persons_columns", return_value={"content_hash", "scraped_at"}), \
             mock.patch.object(scrape_to_tidb, "compute_content_hash", return_value="same-hash"):
            self.assertTrue(scrape_to_tidb.insert_person(db, person()))
        db.commit.assert_called_once()

    def test_get_db_never_guesses_another_port_or_password(self):
        target = {
            "host": "127.0.0.1", "port": 4123, "user": "tester",
            "password": "synthetic-test-secret", "database": "test_db", "autocommit": False,
        }
        with mock.patch.object(scrape_to_tidb, "TIDB_CONFIG", target), \
             mock.patch.object(scrape_to_tidb.mysql.connector, "connect", side_effect=RuntimeError("offline")) as connect:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                scrape_to_tidb.get_db()
        connect.assert_called_once_with(**target)

    def test_ingest_response_does_not_report_unwritten_person_as_success(self):
        url = "https://example.invalid/find/person/person-test"
        page = SimpleNamespace(status=200, url=url, html="<html><body>profile</body></html>")
        with mock.patch.object(scrape_to_tidb, "parse_person", return_value=person(False)), \
             mock.patch.object(scrape_to_tidb, "insert_person", return_value=False):
            with self.assertRaises(scrape_to_tidb.ScrapeError) as raised:
                scrape_to_tidb.ingest_response(page, url, mock.Mock())
        self.assertEqual(raised.exception.bucket, "no_phone")


class StartupSafetyTests(unittest.TestCase):
    def test_direct_worker_mode_is_rejected_before_claim(self):
        with self.assertRaisesRegex(ValueError, "直接内存批量写库模式已禁用"):
            protocol_worker.ProtocolWorker(fakeredis.FakeRedis(), decoupled_ingest=False)

    def test_worker_requires_fresh_bulk_readiness_and_matching_db_target(self):
        worker = protocol_worker.ProtocolWorker(fakeredis.FakeRedis(), decoupled_ingest=True)
        with mock.patch.object(protocol_worker, "bulk_ingest_status", return_value={"rate_available": False}):
            self.assertFalse(worker._bulk_ingester_ready(force=True))
        with mock.patch.object(protocol_worker, "bulk_ingest_status", return_value={
            "rate_available": True, "db_target": "wrong-target",
        }):
            with self.assertRaisesRegex(RuntimeError, "数据库目标不一致"):
                worker._bulk_ingester_ready(force=True)
        with mock.patch.object(protocol_worker, "bulk_ingest_status", return_value={
            "rate_available": True, "db_target": scrape_to_tidb.db_target_fingerprint(),
        }):
            self.assertTrue(worker._bulk_ingester_ready(force=True))

    def test_runner_waits_for_its_own_ingester_not_a_stale_other_pid(self):
        manager = multi_worker_runner.MultiWorkerManager(1, 10)
        manager.ingester_process = SimpleNamespace(pid=12345, poll=lambda: None)
        statuses = [
            {"rate_available": True, "db_target": scrape_to_tidb.db_target_fingerprint(), "pid": 99999},
            {"rate_available": True, "db_target": scrape_to_tidb.db_target_fingerprint(), "pid": 12345},
        ]
        with mock.patch.object(multi_worker_runner.redis, "Redis"), \
             mock.patch.object(multi_worker_runner, "bulk_ingest_status", side_effect=statuses) as status, \
             mock.patch.object(multi_worker_runner.time, "sleep"):
            manager._wait_for_ingester_ready(timeout_sec=1)
        self.assertEqual(status.call_count, 2)

    def test_cluster_direct_mode_rejected_without_launch(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        with mock.patch.object(tps_control, "_start_process") as launch:
            result = tps_control.start_cluster(r, decoupled=False)
        self.assertFalse(result["ok"])
        launch.assert_not_called()


class BatchIngesterTruthTests(unittest.TestCase):
    def test_only_persisted_items_reach_success_callback(self):
        succeeded, failed = [], []
        ingester = batch_ingest.BatchIngester(
            on_success=lambda jobs: succeeded.extend(jobs),
            on_failure=lambda job, error: failed.append((job, str(error))),
        )
        db = mock.Mock()
        ingester._get_active_db = mock.Mock(return_value=db)
        valid_job, empty_job = {"id": "valid"}, {"id": "empty"}
        with mock.patch.object(batch_ingest, "_persons_columns", return_value={
            "person_id", "full_name", "primary_phone", "current_address",
        }):
            ingester._flush_sync([(person(), valid_job), (person(False), empty_job)])

        self.assertEqual(succeeded, [valid_job])
        self.assertEqual(failed, [(empty_job, "no_phone")])
        db.commit.assert_called_once()
        rows = db.cursor.return_value.executemany.call_args_list[0].args[1]
        self.assertEqual(rows[0]["current_address"], "Example address")

    def test_batch_failure_fallback_does_not_ack_skipped_item(self):
        succeeded, failed = [], []
        ingester = batch_ingest.BatchIngester(
            on_success=lambda jobs: succeeded.extend(jobs),
            on_failure=lambda job, error: failed.append((job, str(error))),
        )
        db = mock.Mock()
        db.cursor.return_value.executemany.side_effect = RuntimeError("simulated DB batch failure")
        ingester._get_active_db = mock.Mock(return_value=db)
        valid_job, empty_job = {"id": "valid"}, {"id": "empty"}
        with mock.patch.object(batch_ingest, "_persons_columns", return_value=set()), \
             mock.patch.object(batch_ingest, "insert_person", side_effect=[True, False]):
            ingester._flush_sync([(person(), valid_job), (person(False), empty_job)])
        self.assertEqual(succeeded, [valid_job])
        self.assertEqual(failed, [(empty_job, "no_phone")])


class BatchIngesterLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_flushes_are_serial_and_stop_waits_for_them(self):
        ingester = batch_ingest.BatchIngester(batch_size=1)
        active = 0
        peak = 0
        handled = []
        lock = threading.Lock()

        def flush_sync(items):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            handled.extend(job["id"] for _, job in items)
            with lock:
                active -= 1

        ingester._flush_sync = flush_sync
        await ingester.start()
        await ingester.add(person(), {"id": "one"})
        await ingester.add(person(), {"id": "two"})
        await ingester.stop()
        self.assertEqual(peak, 1)
        self.assertEqual(handled, ["one", "two"])


class ReliableBufferTests(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.daemon = bulk_ingester_daemon.BulkIngesterDaemon(self.redis, batch_size=50)
        self.daemon.batch_size = 2
        self.db = mock.Mock()
        self.daemon._get_active_db = mock.Mock(return_value=self.db)

    def enqueue(self, data, job_id="j1"):
        raw = json.dumps({"data": data, "job": {"id": job_id, "person_id": data.get("person_id")}})
        self.redis.lpush(bulk_ingester_daemon.BUFFER_KEY, raw)
        return raw

    def test_bulk_readiness_snapshot_requires_live_database_and_identifies_target(self):
        self.daemon._db_ready = True
        self.daemon._publish_rate_if_due(force=True)
        ready = tps_control.bulk_ingest_status(self.redis)
        self.assertTrue(ready["rate_available"])
        self.assertEqual(ready["db_target"], scrape_to_tidb.db_target_fingerprint())
        self.assertEqual(ready["pid"], bulk_ingester_daemon.os.getpid())
        self.daemon._db_ready = False
        self.daemon._publish_rate_if_due(force=True)
        self.assertFalse(tps_control.bulk_ingest_status(self.redis)["rate_available"])

    def test_claim_is_recoverable_after_interruption(self):
        raw = self.enqueue(person())
        self.assertEqual(self.daemon._claim_raw_batch(), [raw])
        self.assertEqual(self.redis.llen(bulk_ingester_daemon.BUFFER_KEY), 0)
        restarted = bulk_ingester_daemon.BulkIngesterDaemon(self.redis, batch_size=50)
        restarted.batch_size = 2
        self.assertEqual(restarted._claim_raw_batch(), [raw])

    def test_protocol_handoff_parks_job_without_expiring_lease_requeue(self):
        claimed_at = time.time()
        job = {
            "id": "j-parked", "person_id": "person-test", "worker_id": "worker-1",
            "claimed_at": claimed_at, "lease_until": claimed_at + 90,
        }
        self.redis.set(f"{tps_queue.JOB_KEY_PREFIX}{job['id']}", json.dumps(job))
        self.redis.lpush(tps_queue.PROCESSING_KEY, job["id"])
        self.redis.zadd(tps_queue.LEASES_KEY, {job["id"]: claimed_at + 90})
        self.redis.sadd(tps_queue.QUEUED_KEY, job["person_id"])

        protocol_worker.park_buffered_job(self.redis, job, person())
        self.assertEqual(self.redis.llen(protocol_worker.BUFFER_KEY), 1)
        self.assertEqual(self.redis.llen(tps_queue.PROCESSING_KEY), 0)
        self.assertIsNone(self.redis.zscore(tps_queue.LEASES_KEY, job["id"]))
        self.assertEqual(tps_queue.recover_expired(self.redis), 0)
        self.assertEqual(self.redis.llen(tps_queue.PENDING_KEY), 0)

        # 入库守护进程崩溃重启后仍能从 processing buffer 接管并最终 ACK。
        raw = self.daemon._claim_raw_batch()[0]
        restarted = bulk_ingester_daemon.BulkIngesterDaemon(self.redis, batch_size=50)
        restarted.batch_size = 2
        restarted._get_active_db = mock.Mock(return_value=self.db)
        with mock.patch.object(bulk_ingester_daemon, "_persons_columns", return_value=set()):
            self.assertEqual(restarted._process_raw_batch([raw]), 1)
        self.assertTrue(self.redis.sismember(tps_queue.SEEN_KEY, job["person_id"]))
        self.assertFalse(self.redis.sismember(tps_queue.QUEUED_KEY, job["person_id"]))

    def test_protocol_handoff_rejects_lost_lease_without_buffering(self):
        job = {"id": "j-lost", "person_id": "person-test", "worker_id": "worker-1", "claimed_at": time.time()}
        self.redis.set(f"{tps_queue.JOB_KEY_PREFIX}{job['id']}", json.dumps(job))
        self.redis.lpush(tps_queue.PROCESSING_KEY, job["id"])
        with self.assertRaises(protocol_worker.BufferHandoffLost):
            protocol_worker.park_buffered_job(self.redis, job, person())
        self.assertEqual(self.redis.llen(protocol_worker.BUFFER_KEY), 0)

    def test_protocol_handoff_rejects_expired_lease_then_recovery_requeues_once(self):
        claimed_at = time.time() - 120
        job = {"id": "j-expired", "person_id": "person-test", "worker_id": "worker-1", "claimed_at": claimed_at}
        self.redis.set(f"{tps_queue.JOB_KEY_PREFIX}{job['id']}", json.dumps(job))
        self.redis.lpush(tps_queue.PROCESSING_KEY, job["id"])
        self.redis.zadd(tps_queue.LEASES_KEY, {job["id"]: claimed_at + 90})
        with self.assertRaises(protocol_worker.BufferHandoffLost):
            protocol_worker.park_buffered_job(self.redis, job, person())
        self.assertEqual(self.redis.llen(protocol_worker.BUFFER_KEY), 0)
        self.assertEqual(tps_queue.recover_expired(self.redis), 1)
        self.assertEqual(self.redis.llen(tps_queue.PENDING_KEY), 1)
        self.assertEqual(self.redis.llen(tps_queue.PROCESSING_KEY), 0)

    def test_recover_and_handoff_race_never_creates_pending_and_buffer_copy(self):
        claimed_at = time.time() - 120
        job = {
            "id": "j-race", "person_id": "person-test", "worker_id": "worker-1",
            "claimed_at": claimed_at, "lease_until": claimed_at + 90, "attempts": 0,
        }
        self.redis.set(f"{tps_queue.JOB_KEY_PREFIX}{job['id']}", json.dumps(job))
        self.redis.lpush(tps_queue.PROCESSING_KEY, job["id"])
        self.redis.zadd(tps_queue.LEASES_KEY, {job["id"]: claimed_at + 90})

        real_pipeline = self.redis.pipeline
        injected = False

        class InterleavingPipeline:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def multi(self):
                nonlocal injected
                if not injected:
                    injected = True
                    # 回收已读旧租约，Worker 恰好续租并交给入库缓冲。
                    self_redis.zadd(tps_queue.LEASES_KEY, {job["id"]: time.time() + 90})
                    protocol_worker.park_buffered_job(self_redis, job, person())
                return self.inner.multi()

        self_redis = self.redis

        def pipeline_factory(*args, **kwargs):
            inner = real_pipeline(*args, **kwargs)
            return inner if injected else InterleavingPipeline(inner)

        with mock.patch.object(self.redis, "pipeline", side_effect=pipeline_factory):
            self.assertEqual(tps_queue.recover_expired(self.redis), 0)
        self.assertTrue(injected)
        self.assertEqual(self.redis.llen(protocol_worker.BUFFER_KEY), 1)
        self.assertEqual(self.redis.llen(tps_queue.PENDING_KEY), 0)
        self.assertEqual(self.redis.llen(tps_queue.PROCESSING_KEY), 0)
        self.assertIsNone(self.redis.zscore(tps_queue.LEASES_KEY, job["id"]))

    def test_no_phone_goes_to_failure_not_ack_or_success(self):
        self.enqueue(person(False))
        raw_items = self.daemon._claim_raw_batch()
        with mock.patch.object(bulk_ingester_daemon, "ack") as ack, \
             mock.patch.object(bulk_ingester_daemon, "nack") as nack, \
             mock.patch.object(bulk_ingester_daemon, "_persons_columns", return_value=set()):
            self.assertEqual(self.daemon._process_raw_batch(raw_items), 1)
        ack.assert_not_called()
        self.assertFalse(nack.call_args.kwargs["retry"])
        self.assertEqual(nack.call_args.args[2], "no_phone")
        self.assertEqual(self.redis.llen(bulk_ingester_daemon.PROCESSING_BUFFER_KEY), 0)
        self.assertEqual(self.redis.get("tps:metrics:counter:success"), None)
        self.db.commit.assert_not_called()

    def test_valid_record_is_counted_only_after_commit_and_ack(self):
        self.enqueue(person())
        raw_items = self.daemon._claim_raw_batch()
        with mock.patch.object(bulk_ingester_daemon, "ack") as ack, \
             mock.patch.object(bulk_ingester_daemon, "_persons_columns", return_value={
                 "person_id", "full_name", "primary_phone", "current_address",
             }):
            self.assertEqual(self.daemon._process_raw_batch(raw_items), 1)
        self.db.commit.assert_called_once()
        ack.assert_called_once()
        self.assertEqual(self.redis.llen(bulk_ingester_daemon.PROCESSING_BUFFER_KEY), 0)
        self.assertEqual(int(self.redis.get("tps:metrics:counter:success")), 1)
        self.assertEqual(int(self.redis.get(bulk_ingester_daemon.BULK_COMMITTED_TOTAL_KEY)), 1)
        self.daemon._last_calc_time = time.time() - 4
        self.daemon._publish_rate_if_due()
        rate = json.loads(self.redis.get(bulk_ingester_daemon.BULK_RATE_KEY))
        self.assertEqual(rate["committed_total"], 1)
        self.assertGreater(rate["qps"], 0)

    def test_failed_ack_keeps_committed_payload_recoverable_without_success_metric(self):
        self.enqueue(person())
        raw_items = self.daemon._claim_raw_batch()
        with mock.patch.object(bulk_ingester_daemon, "ack", side_effect=RuntimeError("Redis ACK unavailable")), \
             mock.patch.object(bulk_ingester_daemon, "_persons_columns", return_value=set()):
            self.assertEqual(self.daemon._process_raw_batch(raw_items), 0)
        self.db.commit.assert_called_once()
        self.assertEqual(self.redis.llen(bulk_ingester_daemon.PROCESSING_BUFFER_KEY), 1)
        self.assertIsNone(self.redis.get("tps:metrics:counter:success"))

    def test_database_outage_retains_parsed_payload_without_consuming_queue_attempts(self):
        self.enqueue(person())
        raw_items = self.daemon._claim_raw_batch()
        self.daemon._get_active_db = mock.Mock(side_effect=RuntimeError("synthetic DB outage"))
        with mock.patch.object(bulk_ingester_daemon, "nack") as nack:
            self.assertEqual(self.daemon._process_raw_batch(raw_items), 0)
        nack.assert_not_called()
        self.assertEqual(self.redis.llen(bulk_ingester_daemon.PROCESSING_BUFFER_KEY), 1)
        self.assertEqual(self.daemon._claim_raw_batch(), raw_items)

    def test_individual_db_failure_after_batch_rollback_keeps_raw(self):
        self.enqueue(person())
        raw_items = self.daemon._claim_raw_batch()
        self.db.cursor.return_value.executemany.side_effect = RuntimeError("synthetic batch outage")
        with mock.patch.object(bulk_ingester_daemon, "_persons_columns", return_value=set()), \
             mock.patch.object(bulk_ingester_daemon, "insert_person", side_effect=RuntimeError("synthetic DB outage")), \
             mock.patch.object(bulk_ingester_daemon, "nack") as nack:
            self.assertEqual(self.daemon._process_raw_batch(raw_items), 0)
        nack.assert_not_called()
        self.assertEqual(self.redis.llen(bulk_ingester_daemon.PROCESSING_BUFFER_KEY), 1)

    def test_bad_json_is_quarantined_instead_of_dropped(self):
        raw = "{not-json"
        self.redis.lpush(bulk_ingester_daemon.BUFFER_KEY, raw)
        self.assertEqual(self.daemon._process_raw_batch(self.daemon._claim_raw_batch()), 1)
        self.assertEqual(self.redis.lrange(bulk_ingester_daemon.INVALID_BUFFER_KEY, 0, -1), [raw])
        self.assertEqual(self.redis.llen(bulk_ingester_daemon.PROCESSING_BUFFER_KEY), 0)


class LiveDiagnosticTruthTests(unittest.TestCase):
    def test_dry_run_and_no_phone_never_touch_database_or_production_metrics(self):
        page = SimpleNamespace(
            status=200, url="https://example.invalid/find/person/person-test",
            html="<html><body>profile</body></html>",
        )
        with mock.patch.object(test_live_scrape.redis, "Redis") as redis_type, \
             mock.patch.object(test_live_scrape, "load_proxy_config", return_value={"tunnel": "http://proxy.invalid:8080"}), \
             mock.patch.object(test_live_scrape, "refresh_sticky_url", return_value="http://proxy.invalid:8080"), \
             mock.patch.object(test_live_scrape.StealthyFetcher, "fetch", return_value=page), \
             mock.patch.object(test_live_scrape, "get_db") as get_db, \
             mock.patch.object(test_live_scrape, "parse_person", return_value=person()), \
             mock.patch.object(sys, "argv", ["test_live_scrape.py", page.url]):
            self.assertEqual(test_live_scrape.main(), 0)
            get_db.assert_not_called()
            redis_type.return_value.incr.assert_not_called()

        with mock.patch.object(test_live_scrape.redis, "Redis") as redis_type, \
             mock.patch.object(test_live_scrape, "load_proxy_config", return_value={"tunnel": "http://proxy.invalid:8080"}), \
             mock.patch.object(test_live_scrape, "refresh_sticky_url", return_value="http://proxy.invalid:8080"), \
             mock.patch.object(test_live_scrape.StealthyFetcher, "fetch", return_value=page), \
             mock.patch.object(test_live_scrape, "get_db") as get_db, \
             mock.patch.object(test_live_scrape, "parse_person", return_value=person(False)), \
             mock.patch.object(sys, "argv", ["test_live_scrape.py", page.url, "--write"]):
            self.assertEqual(test_live_scrape.main(), 1)
            get_db.assert_not_called()
            redis_type.return_value.incr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
