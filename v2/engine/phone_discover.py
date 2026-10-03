# -*- coding: utf-8 -*-
"""
TruePeopleSearch 全美电话顺序反查发生器 (Phone Feeder)
标准接入 tps_queue 协议规范，生成标准 Job 注入 Redis，驱动 32 路 Worker 全速消费
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
SCRIPTS = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from tps_env import load_project_env
load_project_env(Path(ROOT), customer_safe=False)

import redis
from phone_plan import (
    blocked_flags,
    connect_redis,
    phone_url,
    prefix_is_cold,
    select_batch,
)
from tps_queue import PENDING_KEY, SEEN_KEY, _dumps, _job_key, new_job

CURSOR_KEY = "tps:phone:cursor"
PAUSE_FILE = Path(ROOT) / "data" / "client.pause"

def get_cursor(r: redis.Redis, default_area: int = 201) -> tuple:
    val = r.get(CURSOR_KEY)
    if val:
        try:
            parts = str(val).split("-")
            return int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            pass
    return default_area, 200, 1

def save_cursor(r: redis.Redis, npa: int, nxx: int, line: int):
    r.set(CURSOR_KEY, f"{npa:03d}-{nxx:03d}-{line:04d}")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def run_feeder(start_area: int = 201, target_queue_size: int = 8000, batch_step: int = 800):
    r = connect_redis()
    print("=" * 65)
    print("   [START] 美国号码发生器已上线")
    print(f"   * 从区号 {start_area} 起，跳过免费号、服务号和空号段")
    print("   * 查到人物后，该人的其他号码一并入库，并不再重复查询")
    print("=" * 65, flush=True)

    while True:
        try:
            if PAUSE_FILE.exists():
                print("[PHONE FEEDER] 已暂停，不生成新号码", flush=True)
                time.sleep(2)
                continue
            current_pending = int(r.llen(PENDING_KEY) or 0)
            npa, nxx, line = get_cursor(r, start_area)

            if current_pending < target_queue_size:
                cold_cache = {}

                def cold(area, office):
                    key = (area, office)
                    if key not in cold_cache:
                        cold_cache[key] = prefix_is_cold(r, area, office)
                    return cold_cache[key]

                numbers, nxt = select_batch(
                    npa,
                    nxx,
                    line,
                    batch_step,
                    blocked=lambda batch: blocked_flags(r, batch, SEEN_KEY),
                    cold=cold,
                )
                if numbers:
                    now = time.time()
                    pipe = r.pipeline(transaction=True)
                    for digits in numbers:
                        url = phone_url(digits)
                        job = new_job(url)
                        job["person_id"] = digits
                        job["url"] = url
                        job["enqueued_at"] = now
                        jid = job["id"]
                        pipe.set(_job_key(jid), _dumps(job))
                        pipe.lpush(PENDING_KEY, jid)
                    pipe.execute()
                if nxt is None:
                    print("[PHONE FEEDER] 美国可分配号段已走完。", flush=True)
                    time.sleep(30)
                    continue
                npa, nxx, line = nxt
                save_cursor(r, npa, nxx, line)
                current_pending = int(r.llen(PENDING_KEY) or 0)
                print(
                    f"[PHONE FEEDER] 注入 {len(numbers)} 个号码查询 | 待抓池: {current_pending:,} 条 | "
                    f"进度: {npa:03d}-{nxx:03d}-{line:04d}",
                    flush=True,
                )
            else:
                print(
                    f"[PHONE FEEDER] 待查池充足 ({current_pending:,} 条) | "
                    f"进度: {npa:03d}-{nxx:03d}-{line:04d}",
                    flush=True,
                )

            time.sleep(2.5)
        except KeyboardInterrupt:
            print("\n[STOP] 电话发生器已退出。")
            break
        except Exception as e:
            print(f"[ERROR] 发生器异常: {e}", flush=True)
            time.sleep(3)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TPS Phone Sequential Feeder")
    parser.add_argument("--start-area", type=int, default=201, help="起始区号")
    args = parser.parse_args()
    run_feeder(start_area=args.start_area)
