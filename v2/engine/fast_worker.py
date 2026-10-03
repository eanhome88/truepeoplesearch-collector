# -*- coding: utf-8 -*-
"""穿云高速采集。不打开本机浏览器。

每天约 50 万条时，按一次请求 3 秒、每人两次请求（号码页 + 人物页）估算并发。
失败不扣基础积分。没有验证挑战时只扣 1 分，并使用动态 IP 和已购流量。
"""

import argparse
import asyncio
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = Path(__file__).resolve().parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

os.environ.setdefault("CLOUDBYPASS_PARTS", "48")
os.environ.setdefault("USE_CLOUDBYPASS", "1")

from tps_env import load_project_env

load_project_env(ROOT, customer_safe=False)
if not os.environ.get("CLOUDBYPASS_APIKEY"):
    load_project_env(ROOT.parent, customer_safe=False)

from cloudbypass_v2 import connect_redis
from scrape_to_tidb import (
    EmptyPageError,
    HttpError,
    ScrapeError,
    ensure_db,
    fetch_cloudbypass_v2,
    ingest_response,
)
from tps_queue import ack, claim, nack, recover_expired, release

DAILY_TARGET = 500_000
PAGE_SEC = 3.0
HOPS = 2
LEASE_SEC = 240
PAUSE_FILE = ROOT / "data" / "client.pause"


def lanes_for_daily(per_day: int = DAILY_TARGET, page_sec: float = PAGE_SEC, hops: int = HOPS) -> int:
    needed = math.ceil(float(per_day) * hops * page_sec / 86400.0)
    return max(8, min(64, needed))


def _paused() -> bool:
    return PAUSE_FILE.exists()


async def _one(slot: int, redis, stop: asyncio.Event):
    db = None
    worker_id = f"fast-{os.getpid()}-{slot}"
    while not stop.is_set():
        if _paused():
            await asyncio.sleep(1)
            continue
        try:
            job = await asyncio.to_thread(claim, redis, worker_id, LEASE_SEC)
        except Exception as exc:
            print(f"[FAST] Redis 不可用: {exc}", flush=True)
            await asyncio.sleep(2)
            continue
        if not job:
            await asyncio.to_thread(recover_expired, redis)
            await asyncio.sleep(0.2)
            continue
        url = str(job.get("url") or "")
        try:
            page = await fetch_cloudbypass_v2(url)
            if page is None:
                await asyncio.to_thread(nack, redis, job, "bypass failed", True)
                continue
            db = await asyncio.to_thread(ensure_db, db)
            await asyncio.to_thread(ingest_response, page, url, db)
            await asyncio.to_thread(ack, redis, job)
        except EmptyPageError:
            await asyncio.to_thread(ack, redis, job)
        except ScrapeError as exc:
            bucket = getattr(exc, "bucket", "")
            if bucket in ("no_phone", "empty"):
                await asyncio.to_thread(ack, redis, job)
            else:
                await asyncio.to_thread(nack, redis, job, str(exc), True)
        except HttpError as exc:
            status = int(getattr(exc, "status", 0) or 0)
            if status in (402, 429):
                await asyncio.to_thread(release, redis, job, str(exc))
                await asyncio.sleep(5 if status == 429 else 30)
            else:
                await asyncio.to_thread(nack, redis, job, str(exc), True)
        except Exception as exc:
            await asyncio.to_thread(nack, redis, job, str(exc), True)


async def run(lanes: int):
    redis = connect_redis()
    stop = asyncio.Event()
    if not os.environ.get("CLOUDBYPASS_APIKEY"):
        print("[FAST] v2\\.env 里没有 CLOUDBYPASS_APIKEY，穿云请求不会发出。", flush=True)
    if not os.environ.get("CLOUDBYPASS_PROXY") and not os.environ.get("PROXY_TUNNEL"):
        print("[FAST] v2\\.env 里没有 CLOUDBYPASS_PROXY 或 PROXY_TUNNEL。", flush=True)
    print(
        f"[FAST] 目标每天约 {DAILY_TARGET:,} 条，并发 {lanes}，不启动本机浏览器",
        flush=True,
    )
    tasks = [asyncio.create_task(_one(i, redis, stop)) for i in range(lanes)]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        raise


def main():
    parser = argparse.ArgumentParser(description="穿云高速采集")
    parser.add_argument("--lanes", type=int, default=0, help="并发请求数，0 表示按每天 50 万条估算")
    args = parser.parse_args()
    lanes = args.lanes if args.lanes > 0 else lanes_for_daily()
    os.environ["CLOUDBYPASS_PARTS"] = str(max(lanes, int(os.environ.get("CLOUDBYPASS_PARTS", "48"))))
    try:
        asyncio.run(run(lanes))
    except KeyboardInterrupt:
        print("[FAST] 已停止", flush=True)


if __name__ == "__main__":
    main()
