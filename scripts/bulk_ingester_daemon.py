#!/usr/bin/env python3
"""
TiDB 3000万级超高吞吐专职批量入库守护进程 (Bulk Ingester Daemon)

设计定位：
1. 彻底解耦抓取与入库：抓取 Worker 解析完直接写入 Redis 缓冲区 (tps:buffer:parsed)，网络连接 0 阻塞。
2. 超大微批事务写入 (500~1000 实体/批，约 4500~9000 行/事务)。
3. 单进程即可达到 4,000 ~ 8,000 行/秒 的 TiDB 写入速率。
4. 事务成功后批量调用 Redis ack，确保不丢单、不重复。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from tps_env import load_project_env
load_project_env(Path(__file__).resolve().parent.parent, customer_safe=False)

import redis

from batch_ingest import BatchIngester
from scrape_to_tidb import (
    _COUNT_FIELDS,
    _PERSON_CORE_FIELDS,
    _child_counts,
    _persons_columns,
    compute_content_hash,
    db_target_fingerprint,
    ensure_db,
    get_db,
    has_usable_phone,
    insert_person,
)
from tps_metrics import get_metrics
from tps_queue import ack, nack

REDIS_HOST = os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None
BUFFER_KEY = "tps:buffer:parsed"
PROCESSING_BUFFER_KEY = "tps:buffer:processing"
INVALID_BUFFER_KEY = "tps:buffer:invalid"
BULK_COMMITTED_TOTAL_KEY = "tps:ingest:bulk:committed_total"
BULK_RATE_KEY = "tps:ingest:bulk:rate"
BULK_RATE_INTERVAL_SEC = 3.0
BULK_RATE_TTL_SEC = 15


def connect_redis() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
        socket_timeout=5,
    )


class BulkIngesterDaemon:
    def __init__(
        self,
        r: redis.Redis,
        batch_size: int = 1000,
        flush_interval_sec: float = 0.5,
    ):
        self.r = r
        self.batch_size = max(50, int(batch_size))
        self.flush_interval_sec = float(flush_interval_sec)
        self.m = get_metrics(r)
        self.stopping = False

        self._db = None
        self._cached_cols = None
        self._total_persons = 0
        self._total_rows = 0
        self._last_calc_time = time.time()
        self._last_calc_persons = 0
        self._db_ready = False
        self._last_db_check = 0.0

    def _get_active_db(self):
        try:
            self._db = ensure_db(self._db)
            self._db_ready = True
            return self._db
        except Exception:
            self._db_ready = False
            self._db = None
            raise

    def _check_db_ready_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_db_check < BULK_RATE_INTERVAL_SEC:
            return
        self._last_db_check = now
        try:
            self._get_active_db()
        except Exception:
            self._db_ready = False

    def _handle_signal(self, sig, _frame):
        signame = signal.Signals(sig).name
        if self.stopping:
            return
        print(f"\n[SIGNAL] 收到 {signame}，正在刷空剩余缓冲并安全停机...")
        self.stopping = True

    def _safe_metric_incr(self, bucket: str) -> None:
        """Metrics must never turn a committed write into a queue retry."""
        try:
            self.m.incr(bucket)
        except Exception as exc:
            print(f"[METRICS] incr {bucket}: {exc}", file=sys.stderr)

    def _remove_processed_raw(self, raw: Optional[str]) -> bool:
        if raw is None:
            return True
        try:
            return bool(self.r.lrem(PROCESSING_BUFFER_KEY, 1, raw))
        except Exception as exc:
            print(f"[BUFFER_RELEASE_FAIL] {exc}", file=sys.stderr)
            return False

    def _record_write_failure(
        self, job: dict, exc: Exception, raw: Optional[str] = None,
        *, retry: bool = True, reason: str = "ingest_err",
    ) -> bool:
        try:
            nack(self.r, job, reason, retry=retry)
        except Exception as nack_exc:
            print(
                f"[BULK_NACK_FAIL] person_id={job.get('person_id')} "
                f"write_err={exc} nack_err={nack_exc}",
                file=sys.stderr,
            )
            return False
        if not self._remove_processed_raw(raw):
            return False
        self._safe_metric_incr("write_fail")
        return True

    def _ack_committed(self, job: dict, raw: Optional[str] = None) -> bool:
        """只在数据库提交、队列 ACK、缓冲释放全部成功后计 success。"""
        try:
            ack(self.r, job)
        except Exception as exc:
            print(f"[BULK_ACK_FAIL] person_id={job.get('person_id')} err={exc}", file=sys.stderr)
            return False
        if not self._remove_processed_raw(raw):
            return False
        try:
            self.r.incr(BULK_COMMITTED_TOTAL_KEY)
        except Exception as exc:
            # 真实入库已完成，指标故障不能让任务再次抓取或伪造失败。
            print(f"[BULK_COUNT_UNAVAILABLE] {type(exc).__name__}", file=sys.stderr)
        self._safe_metric_incr("success")
        return True

    def _publish_rate_if_due(self, *, force: bool = False, log: bool = False) -> None:
        now = time.time()
        elapsed = now - self._last_calc_time
        if not force and elapsed < BULK_RATE_INTERVAL_SEC:
            return
        committed_delta = self._total_persons - self._last_calc_persons
        qps = committed_delta / elapsed if elapsed > 0 else 0.0
        try:
            total = int(self.r.get(BULK_COMMITTED_TOTAL_KEY) or 0)
            snapshot = {
                "qps": round(max(0.0, qps), 3),
                "committed_total": total,
                "updated_at": now,
                "pid": os.getpid(),
                "db_ready": self._db_ready,
                "db_target": db_target_fingerprint(),
            }
            self.r.set(BULK_RATE_KEY, json.dumps(snapshot), ex=BULK_RATE_TTL_SEC)
        except Exception as exc:
            print(f"[BULK_RATE_UNAVAILABLE] {type(exc).__name__}", file=sys.stderr)
        if log:
            print(
                f"[BULK_INGEST] 已确认入库: {qps:6.1f} 人/秒 | "
                f"本进程累计确认: {self._total_persons:,}"
            )
        self._last_calc_persons = self._total_persons
        self._last_calc_time = now

    def _claim_raw_batch(self) -> List[str]:
        """优先恢复上次未确认的缓冲，再原子地移入处理中列表。"""
        outstanding = self.r.lrange(PROCESSING_BUFFER_KEY, 0, self.batch_size - 1)
        if outstanding:
            return outstanding
        pipe = self.r.pipeline(transaction=True)
        for _ in range(self.batch_size):
            pipe.rpoplpush(BUFFER_KEY, PROCESSING_BUFFER_KEY)
        return [raw for raw in pipe.execute() if raw is not None]

    def _quarantine_raw(self, raw: str) -> bool:
        """损坏载荷保存在隔离列表，绝不静默丢弃。"""
        try:
            pipe = self.r.pipeline(transaction=True)
            pipe.lpush(INVALID_BUFFER_KEY, raw)
            pipe.lrem(PROCESSING_BUFFER_KEY, 1, raw)
            pipe.execute()
            return True
        except Exception as exc:
            print(f"[BUFFER_QUARANTINE_FAIL] {exc}", file=sys.stderr)
            return False

    def _process_raw_batch(self, raw_items: List[str]) -> int:
        items: List[Tuple[dict, dict, str]] = []
        handled = 0
        for raw in raw_items:
            try:
                parsed = json.loads(raw)
                data = parsed.get("data")
                job = parsed.get("job")
            except (TypeError, ValueError, AttributeError):
                data, job = None, None
            if not isinstance(data, dict) or not isinstance(job, dict) or not job.get("id"):
                if isinstance(job, dict) and job.get("id"):
                    self._record_write_failure(
                        job, ValueError("invalid_buffer_payload"), raw,
                        retry=False, reason="invalid_buffer_payload",
                    )
                if self._quarantine_raw(raw):
                    handled += 1
                continue
            items.append((data, job, raw))
        if items:
            handled += self._flush_batch(items)
        return handled

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        print("============================================================")
        print(f"[BULK_INGEST] 专职超高吞吐入库守护进程启动成功 (PID: {os.getpid()})")
        print(f"[BULK_INGEST] 单批微批大小: {self.batch_size} 实体 (预估 ~{self.batch_size*9} 行/事务)")
        print(f"[BULK_INGEST] 最长等待间隔: {self.flush_interval_sec} 秒")
        print(f"[BULK_INGEST] 监听缓冲队列: {BUFFER_KEY}")
        print("============================================================")

        self._check_db_ready_if_due()
        self._publish_rate_if_due(force=True)

        while not self.stopping:
            try:
                raw_items = self._claim_raw_batch()
                if not raw_items:
                    self._check_db_ready_if_due()
                    self._publish_rate_if_due()
                    time.sleep(0.05)
                    continue
                if not self._process_raw_batch(raw_items):
                    time.sleep(1)
                self._publish_rate_if_due()

            except Exception as e:
                print(f"[BULK_ERR] 主循环异常: {e}", file=sys.stderr)
                time.sleep(1)

        # 优雅停机：刷空剩余所有缓冲
        self._drain_all()
        remaining = self.r.llen(BUFFER_KEY) + self.r.llen(PROCESSING_BUFFER_KEY)
        print(f"[BULK_INGEST] 守护进程退出；未确认缓冲={remaining}（保留待恢复）")

    def _flush_batch(self, items: List[Tuple[dict, dict, str]]) -> int:
        """执行单次微批；返回已从可靠缓冲确认/隔离的条数。"""
        start_t = time.time()
        try:
            db = self._get_active_db()
            cursor = db.cursor()
        except Exception as exc:
            # 数据库暂不可用时保留已解析的原始载荷，不能通过 NACK 消耗抓取尝试次数。
            self._db_ready = False
            print(f"[BULK_DB_UNAVAILABLE] type={type(exc).__name__}; parsed buffer retained", file=sys.stderr)
            return 0

        try:
            if self._cached_cols is None:
                self._cached_cols = _persons_columns(cursor)
            cols = self._cached_cols

            persons_rows = []
            committed_items = []
            invalid_items = []
            aliases_rows = []
            cur_addr_rows = []
            prev_addr_rows = []
            phones_rows = []
            emails_rows = []

            for data, job, raw in items:
                person_id = data.get("person_id")
                if not person_id or not data.get("full_name"):
                    invalid_items.append((job, raw, "invalid_person"))
                    continue

                if not has_usable_phone(data):
                    invalid_items.append((job, raw, "no_phone"))
                    continue

                committed_items.append((job, raw))

                content_hash = compute_content_hash(data)
                counts = _child_counts(data)

                row_dict = {
                    f: data.get("current_address_text") if f == "current_address" else data.get(f)
                    for f in _PERSON_CORE_FIELDS if not cols or f in cols
                }
                if "content_hash" in cols:
                    row_dict["content_hash"] = content_hash
                for c in _COUNT_FIELDS:
                    if c in cols:
                        row_dict[c] = counts[c]
                persons_rows.append(row_dict)

                for a in (data.get("aliases") or []):
                    if isinstance(a, dict):
                        name = a.get("alias_name")
                    elif isinstance(a, str):
                        name = a
                    else:
                        name = None
                    if name:
                        aliases_rows.append((person_id, name))

                ca = data.get("current_address")
                if isinstance(ca, dict) and any(ca.get(k) is not None for k in (
                    "street", "unit", "city", "state", "zip_code", "county",
                    "estimated_value", "bathrooms", "square_feet", "year_built", "hoa_fee_monthly",
                )):
                    cur_addr_rows.append((
                        person_id, ca.get("street"), ca.get("unit"), ca.get("city"),
                        ca.get("state"), ca.get("zip_code"), ca.get("county"),
                        ca.get("estimated_value"), ca.get("bathrooms"),
                        ca.get("square_feet"), ca.get("year_built"), ca.get("hoa_fee_monthly"),
                    ))

                for a in (data.get("previous_addresses") or []):
                    if isinstance(a, dict):
                        prev_addr_rows.append((
                            person_id, a.get("street"), a.get("city"), a.get("state"),
                            a.get("zip_code"), a.get("county"),
                        ))

                for p in (data.get("phone_numbers") or []):
                    if isinstance(p, dict):
                        num = p.get("phone_number")
                        if num:
                            phones_rows.append((
                                person_id, num, p.get("line_type"), p.get("carrier"),
                                p.get("is_primary"), p.get("last_reported"),
                            ))

                for e in (data.get("emails") or []):
                    if isinstance(e, dict):
                        mail = e.get("email")
                        if mail:
                            emails_rows.append((person_id, mail))

            # 批量执行
            if persons_rows:
                fields = list(persons_rows[0].keys())
                col_sql = ", ".join(fields)
                placeholders = ", ".join(f"%({f})s" for f in fields)
                update_parts = [f"{f}=VALUES({f})" for f in fields if f != "person_id"]
                if "scraped_at" in cols:
                    update_parts.append("scraped_at=CURRENT_TIMESTAMP")

                cursor.executemany(
                    f"INSERT INTO persons ({col_sql}) VALUES ({placeholders}) "
                    f"ON DUPLICATE KEY UPDATE {', '.join(update_parts)}",
                    persons_rows,
                )

            if aliases_rows:
                cursor.executemany(
                    "INSERT INTO aliases (person_id, alias_name) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE alias_name=VALUES(alias_name)",
                    aliases_rows,
                )

            if cur_addr_rows:
                cursor.executemany(
                    """
                    INSERT INTO current_addresses
                        (person_id, street, unit, city, state, zip_code, county,
                         estimated_value, bathrooms, square_feet, year_built, hoa_fee_monthly)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        street=VALUES(street), unit=VALUES(unit), city=VALUES(city),
                        state=VALUES(state), zip_code=VALUES(zip_code), county=VALUES(county),
                        estimated_value=VALUES(estimated_value), bathrooms=VALUES(bathrooms),
                        square_feet=VALUES(square_feet), year_built=VALUES(year_built),
                        hoa_fee_monthly=VALUES(hoa_fee_monthly)
                    """,
                    cur_addr_rows,
                )

            if prev_addr_rows:
                cursor.executemany(
                    """
                    INSERT INTO previous_addresses
                        (person_id, street, city, state, zip_code, county)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        city=VALUES(city), state=VALUES(state), county=VALUES(county)
                    """,
                    prev_addr_rows,
                )

            if phones_rows:
                cursor.executemany(
                    """
                    INSERT INTO phone_numbers
                        (person_id, phone_number, line_type, carrier, is_primary, last_reported)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        line_type=VALUES(line_type), carrier=VALUES(carrier),
                        is_primary=VALUES(is_primary), last_reported=VALUES(last_reported)
                    """,
                    phones_rows,
                )

            if emails_rows:
                cursor.executemany(
                    "INSERT INTO email_addresses (person_id, email) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE email=VALUES(email)",
                    emails_rows,
                )

            if persons_rows:
                db.commit()

            # DB commit、队列 ACK 和可靠缓冲释放全部成功后才计 success。
            acked_count = 0
            handled = 0
            for job, raw in committed_items:
                if self._ack_committed(job, raw):
                    acked_count += 1
                    handled += 1
            for job, raw, reason in invalid_items:
                if self._record_write_failure(job, ValueError(reason), raw, retry=False, reason=reason):
                    handled += 1

            # 统计与耗时
            total_rows_batch = (
                len(persons_rows) + len(aliases_rows) + len(cur_addr_rows)
                + len(prev_addr_rows) + len(phones_rows) + len(emails_rows)
            )
            cost_ms = (time.time() - start_t) * 1000
            self._total_persons += acked_count
            self._total_rows += total_rows_batch

            self._publish_rate_if_due(log=True)
            return handled

        except Exception as exc:
            self._db_ready = False
            try:
                db.rollback()
            except Exception as rollback_exc:
                print(
                    f"[BULK_ROLLBACK_FAIL] type={type(rollback_exc).__name__}; parsed buffer retained",
                    file=sys.stderr,
                )
                return 0
            print(f"[BULK_FAIL] 批量写入异常，逐条重试降级: {type(exc).__name__}", file=sys.stderr)
            handled = 0
            acked_count = 0
            for data, job, raw in items:
                try:
                    persisted = insert_person(db, data)
                except Exception as s_exc:
                    # 可恢复的 DB/IO 故障原地等待，避免 retries 达上限后丢进 DLQ。
                    print(
                        f"[BULK_RETRY_LATER] job={job.get('id')} type={type(s_exc).__name__}",
                        file=sys.stderr,
                    )
                    continue
                if persisted:
                    self._db_ready = True
                    if self._ack_committed(job, raw):
                        handled += 1
                        acked_count += 1
                else:
                    if not data.get("person_id") or not data.get("full_name"):
                        reason = "invalid_person"
                    elif not has_usable_phone(data):
                        reason = "no_phone"
                    else:
                        reason = None
                    if reason and self._record_write_failure(job, ValueError(reason), raw, retry=False, reason=reason):
                        handled += 1
            self._total_persons += acked_count
            self._publish_rate_if_due(log=True)
            return handled
        finally:
            cursor.close()

    def _drain_all(self) -> None:
        print("[BULK_INGEST] 正在处理剩余可靠缓冲...")
        while True:
            raw_items = self._claim_raw_batch()
            if not raw_items:
                break
            if not self._process_raw_batch(raw_items):
                print("[BULK_INGEST] 入库或 ACK 未确认，保留缓冲供下次启动恢复", file=sys.stderr)
                break


def main():
    parser = argparse.ArgumentParser(description="TiDB 3000万级批量入库守护进程")
    parser.add_argument("--batch-size", type=int, default=1000, help="单批入库实体数 (默认 1000)")
    parser.add_argument("--flush-interval", type=float, default=0.5, help="最长刷新时间(秒)")
    args = parser.parse_args()

    r = connect_redis()
    daemon = BulkIngesterDaemon(
        r=r,
        batch_size=args.batch_size,
        flush_interval_sec=args.flush_interval,
    )
    daemon.run()


if __name__ == "__main__":
    main()
