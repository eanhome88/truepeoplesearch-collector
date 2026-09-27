#!/usr/bin/env python3
"""
TiDB 高性能批量入库模块 (Batch Ingester)

核心优化：
1. 内存聚合缓冲区 (Buffer Queue)，攒满 N 条或到达 T 秒自动触发批量入库。
2. 将原本每条人物 6-8 次单独 SQL 事务，聚合成 6 次批量多行操作 (executemany)。
3. 事务数下降 95% 以上，彻底消除 TiDB 连接争用与锁冲突。
4. 失败自动降级：批量入库遇到异常时，自动回滚并逐条重试该批次，隔离坏数据，确保其余数据安全入库。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

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


class BatchIngester:
    """带自动定时/定量 Flush 的 TiDB 批量入库器"""

    def __init__(
        self,
        batch_size: int = 50,
        flush_interval_sec: float = 1.0,
        on_success: Optional[Callable[[List[Any]], Any]] = None,
        on_failure: Optional[Callable[[Any, Exception], Any]] = None,
    ):
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self.on_success = on_success
        self.on_failure = on_failure

        self._queue: asyncio.Queue[Tuple[dict, Any]] = asyncio.Queue()
        self._flush_task: Optional[asyncio.Task] = None
        self._stopped = False
        self._cached_cols: Optional[Set[str]] = None
        self._db = None

    async def start(self) -> None:
        """启动后台批量刷写协程"""
        if self._flush_task is None:
            self._stopped = False
            self._flush_task = asyncio.create_task(self._flusher_loop())

    async def stop(self) -> None:
        """停止刷写，并处理完当前队列中所有剩余数据"""
        self._stopped = True
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        # 刷空剩余数据
        await self._flush_all()
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    async def add(self, data: dict, job: Any = None) -> None:
        """将解析后的人物数据加入待入库队列"""
        await self._queue.put((data, job))
        if self._queue.qsize() >= self.batch_size:
            # 队列达到批量上限，立即唤醒刷盘
            asyncio.create_task(self.flush())

    async def flush(self) -> None:
        """异步执行一次缓冲区刷盘"""
        items: List[Tuple[dict, Any]] = []
        while not self._queue.empty() and len(items) < self.batch_size * 2:
            try:
                item = self._queue.get_nowait()
                items.append(item)
            except asyncio.QueueEmpty:
                break

        if not items:
            return

        # 在线程池中执行同步数据库 I/O，不阻塞 async 事件循环
        await asyncio.to_thread(self._flush_sync, items)

    async def _flush_all(self) -> None:
        """同步刷尽所有剩余队列"""
        items: List[Tuple[dict, Any]] = []
        while not self._queue.empty():
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if items:
            await asyncio.to_thread(self._flush_sync, items)

    async def _flusher_loop(self) -> None:
        """定期定时刷写循环"""
        while not self._stopped:
            try:
                await asyncio.sleep(self.flush_interval_sec)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[INGEST_LOOP_ERR] {e}", file=sys.stderr)

    def _get_active_db(self):
        self._db = ensure_db(self._db)
        return self._db

    def _flush_sync(self, items: List[Tuple[dict, Any]]) -> None:
        """同步批量执行 TiDB 写入"""
        if not items:
            return

        db = self._get_active_db()
        cursor = db.cursor()

        try:
            if self._cached_cols is None:
                self._cached_cols = _persons_columns(cursor)
            cols = self._cached_cols

            # 1. 组装主表批量数据
            persons_rows = []
            aliases_rows = []
            cur_addr_rows = []
            prev_addr_rows = []
            phones_rows = []
            emails_rows = []

            for data, _ in items:
                person_id = data.get("person_id")
                if not person_id or not data.get("full_name"):
                    continue

                content_hash = compute_content_hash(data)
                counts = _child_counts(data)

                row_dict = {f: data.get(f) for f in _PERSON_CORE_FIELDS if not cols or f in cols}
                if "content_hash" in cols:
                    row_dict["content_hash"] = content_hash
                for c in _COUNT_FIELDS:
                    if c in cols:
                        row_dict[c] = counts[c]
                persons_rows.append(row_dict)

                # 别名
                for a in (data.get("aliases") or []):
                    name = a.get("alias_name")
                    if name:
                        aliases_rows.append((person_id, name))

                # 当前地址
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

                # 过往地址
                for a in (data.get("previous_addresses") or []):
                    prev_addr_rows.append((
                        person_id, a.get("street"), a.get("city"), a.get("state"),
                        a.get("zip_code"), a.get("county"),
                    ))

                # 电话
                for p in (data.get("phone_numbers") or []):
                    num = p.get("phone_number")
                    if num:
                        phones_rows.append((
                            person_id, num, p.get("line_type"), p.get("carrier"),
                            p.get("is_primary"), p.get("last_reported"),
                        ))

                # 邮箱
                for e in (data.get("emails") or []):
                    mail = e.get("email")
                    if mail:
                        emails_rows.append((person_id, mail))

            # 2. 批量执行 SQL (单个事务)
            if persons_rows:
                sample = persons_rows[0]
                fields = list(sample.keys())
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

            # 成功回调
            if self.on_success:
                jobs = [job for _, job in items if job is not None]
                if jobs:
                    self.on_success(jobs)

        except Exception as exc:
            db.rollback()
            print(f"[INGEST_BATCH_FAIL] 批量写入失败，自动降级为逐条写入: {exc}", file=sys.stderr)
            # 降级：逐条写入以隔离失败项
            for data, job in items:
                try:
                    insert_person(db, data)
                    if self.on_success and job is not None:
                        self.on_success([job])
                except Exception as single_exc:
                    if self.on_failure and job is not None:
                        self.on_failure(job, single_exc)
        finally:
            cursor.close()
