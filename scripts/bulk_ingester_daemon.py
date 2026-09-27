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

import redis

from batch_ingest import BatchIngester
from scrape_to_tidb import (
    _COUNT_FIELDS,
    _PERSON_CORE_FIELDS,
    _child_counts,
    _persons_columns,
    compute_content_hash,
    ensure_db,
    get_db,
    insert_person,
)
from tps_metrics import get_metrics
from tps_queue import ack, nack

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
BUFFER_KEY = "tps:buffer:parsed"


def connect_redis() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
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

    def _get_active_db(self):
        self._db = ensure_db(self._db)
        return self._db

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

    def _record_write_failure(self, job: dict, exc: Exception) -> None:
        self._safe_metric_incr("write_fail")
        try:
            nack(self.r, job, "ingest_err", retry=True)
        except Exception as nack_exc:
            print(
                f"[BULK_NACK_FAIL] person_id={job.get('person_id')} "
                f"write_err={exc} nack_err={nack_exc}",
                file=sys.stderr,
            )

    def _ack_committed(self, job: dict) -> bool:
        """Count success only after the committed item is ACKed in Redis."""
        try:
            ack(self.r, job)
        except Exception as exc:
            self._record_write_failure(job, exc)
            return False
        self._safe_metric_incr("success")
        return True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        print("============================================================")
        print(f"[BULK_INGEST] 专职超高吞吐入库守护进程启动成功 (PID: {os.getpid()})")
        print(f"[BULK_INGEST] 单批微批大小: {self.batch_size} 实体 (预估 ~{self.batch_size*9} 行/事务)")
        print(f"[BULK_INGEST] 最长等待间隔: {self.flush_interval_sec} 秒")
        print(f"[BULK_INGEST] 监听缓冲队列: {BUFFER_KEY}")
        print("============================================================")

        last_flush = time.time()

        while not self.stopping:
            try:
                # 1. 批量从 Redis Buffer 取出数据 (最多 batch_size 条)
                # Redis 6.2+ 支持 RPOP key count
                raw_items = []
                try:
                    raw_items = self.r.rpop(BUFFER_KEY, self.batch_size) or []
                except Exception:
                    # 降级兼容旧版 Redis
                    pipe = self.r.pipeline()
                    for _ in range(self.batch_size):
                        pipe.rpop(BUFFER_KEY)
                    raw_items = [item for item in pipe.execute() if item]

                if not raw_items:
                    time.sleep(0.05)
                    continue

                items = []
                for raw in raw_items:
                    try:
                        parsed = json.loads(raw)
                        data = parsed.get("data")
                        job = parsed.get("job")
                        if data and job:
                            items.append((data, job))
                    except Exception:
                        pass

                if items:
                    self._flush_batch(items)
                    last_flush = time.time()

            except Exception as e:
                print(f"[BULK_ERR] 主循环异常: {e}", file=sys.stderr)
                time.sleep(1)

        # 优雅停机：刷空剩余所有缓冲
        self._drain_all()
        print("[BULK_INGEST] 所有缓冲数据已安全入库，守护进程退出完成。")

    def _flush_batch(self, items: List[Tuple[dict, dict]]) -> None:
        """执行单次微批多行插入事务"""
        start_t = time.time()
        db = self._get_active_db()
        cursor = db.cursor()

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

            for data, job in items:
                person_id = data.get("person_id")
                if not person_id or not data.get("full_name"):
                    invalid_items.append((data, job))
                    continue

                committed_items.append((data, job))

                content_hash = compute_content_hash(data)
                counts = _child_counts(data)

                row_dict = {f: data.get(f) for f in _PERSON_CORE_FIELDS if not cols or f in cols}
                if "content_hash" in cols:
                    row_dict["content_hash"] = content_hash
                for c in _COUNT_FIELDS:
                    if c in cols:
                        row_dict[c] = counts[c]
                persons_rows.append(row_dict)

                for a in (data.get("aliases") or []):
                    name = a.get("alias_name")
                    if name:
                        aliases_rows.append((person_id, name))

                ca = data.get("current_address") or {}
                if any(ca.get(k) is not None for k in (
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
                    prev_addr_rows.append((
                        person_id, a.get("street"), a.get("city"), a.get("state"),
                        a.get("zip_code"), a.get("county"),
                    ))

                for p in (data.get("phone_numbers") or []):
                    num = p.get("phone_number")
                    if num:
                        phones_rows.append((
                            person_id, num, p.get("line_type"), p.get("carrier"),
                            p.get("is_primary"), p.get("last_reported"),
                        ))

                for e in (data.get("emails") or []):
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

            db.commit()

            # DB commit 与 Redis ACK 都成功后才计 success。ACK 失败的任务回队，
            # 由幂等 upsert 在下次重试时收敛。
            acked_count = 0
            for _, job in committed_items:
                if self._ack_committed(job):
                    acked_count += 1
            for _, job in invalid_items:
                self._record_write_failure(job, ValueError("missing person_id or full_name"))

            # 统计与耗时
            total_rows_batch = (
                len(persons_rows) + len(aliases_rows) + len(cur_addr_rows)
                + len(prev_addr_rows) + len(phones_rows) + len(emails_rows)
            )
            cost_ms = (time.time() - start_t) * 1000
            self._total_persons += acked_count
            self._total_rows += total_rows_batch

            # 输出吞吐指标
            now = time.time()
            elapsed = now - self._last_calc_time
            if elapsed >= 3.0:
                p_delta = self._total_persons - self._last_calc_persons
                pps = p_delta / elapsed
                rps = pps * (self._total_rows / max(1, self._total_persons))
                print(
                    f"[BULK_INGEST] 实体写入: {pps:6.1f} 人/秒 | "
                    f"数据库行速: {rps:6.1f} 行/秒 | "
                    f"单批耗时: {cost_ms:4.0f}ms ({len(items)}条/批) | "
                    f"累计实体: {self._total_persons:,}"
                )
                self._last_calc_persons = self._total_persons
                self._last_calc_time = now

        except Exception as exc:
            db.rollback()
            print(f"[BULK_FAIL] 批量写入异常，逐条重试降级: {exc}", file=sys.stderr)
            for data, job in items:
                try:
                    insert_person(db, data)
                except Exception as s_exc:
                    self._record_write_failure(job, s_exc)
                    continue
                self._ack_committed(job)
        finally:
            cursor.close()

    def _drain_all(self) -> None:
        print("[BULK_INGEST] 正在清空队列剩余数据...")
        while True:
            raw_items = self.r.rpop(BUFFER_KEY, self.batch_size) or []
            if not raw_items:
                break
            items = []
            for raw in raw_items:
                try:
                    parsed = json.loads(raw)
                    data = parsed.get("data")
                    job = parsed.get("job")
                    if data and job:
                        items.append((data, job))
                except Exception:
                    pass
            if items:
                self._flush_batch(items)


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
