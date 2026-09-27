# -*- coding: utf-8 -*-
"""
TruePeopleSearch 全美电话顺序反查发生器 (Phone Feeder)
标准接入 tps_queue 协议规范，生成标准 Job 注入 Redis，驱动 32 路 Worker 全速消费
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
SCRIPTS = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import redis
from tps_queue import PENDING_KEY, _dumps, _job_key, new_job

CURSOR_KEY = "tps:phone:cursor"
BASE_URL = "https://www.truepeoplesearch.com/resultphone?phoneno="

def connect_redis() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True
    )

def is_valid_nxx(nxx: int) -> bool:
    """过滤无效局号：首位不能为 0 或 1，尾两位不能是 11 (如 911/411/211)"""
    if nxx < 200 or nxx > 999:
        return False
    s = str(nxx)
    if s[1:] == "11":
        return False
    return True

def get_cursor(r: redis.Redis, default_area: int = 201) -> tuple:
    val = r.get(CURSOR_KEY)
    if val:
        try:
            parts = str(val).split("-")
            return int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            pass
    return default_area, 200, 0

def save_cursor(r: redis.Redis, npa: int, nxx: int, line: int):
    r.set(CURSOR_KEY, f"{npa:03d}-{nxx:03d}-{line:04d}")

def run_feeder(start_area: int = 201, target_queue_size: int = 2000, batch_step: int = 300):
    r = connect_redis()
    print("=" * 65)
    print("   🚀 TruePeopleSearch 全美电话号码顺序发生器已上线")
    print(f"   • 当前区号起点: {start_area} (例如: 201-200-0000 起)")
    print("   • 调度协议: 标准 tps:job 租约队列管道 (与 32路 Worker 完美协同)")
    print("=" * 65, flush=True)

    while True:
        try:
            current_pending = int(r.llen(PENDING_KEY) or 0)
            npa, nxx, line = get_cursor(r, start_area)

            # 保持队列有 1,000 ~ 2,500 个任务供 32 路 Worker 持续消费
            if current_pending < target_queue_size:
                to_push = []
                while len(to_push) < batch_step:
                    if not is_valid_nxx(nxx):
                        nxx += 1
                        line = 0
                        if nxx > 999:
                            npa += 1
                            nxx = 200
                        continue

                    phone_10 = f"{npa:03d}{nxx:03d}{line:04d}"
                    url = f"{BASE_URL}{phone_10}"
                    to_push.append((url, phone_10))

                    line += 1
                    if line > 9999:
                        line = 0
                        nxx += 1
                        if nxx > 999:
                            npa += 1
                            nxx = 200

                # 按照 tps_queue 协议批量创建标准 Job
                now = time.time()
                pipe = r.pipeline(transaction=True)
                for url, pid in to_push:
                    job = new_job(url)
                    job["person_id"] = pid
                    job["url"] = url
                    job["enqueued_at"] = now
                    jid = job["id"]
                    pipe.set(_job_key(jid), _dumps(job))
                    pipe.lpush(PENDING_KEY, jid)
                pipe.execute()

                save_cursor(r, npa, nxx, line)
                current_pending = int(r.llen(PENDING_KEY) or 0)
                print(f"[PHONE FEEDER] 成功生成注入 {len(to_push)} 个电话任务 | 待抓池: {current_pending:,} 条 | 当前号段进度: {npa:03d}-{nxx:03d}-{line:04d}", flush=True)
            else:
                # 队列充足时的状态心跳
                print(f"[PHONE FEEDER] 待查池任务充沛 ({current_pending:,} 条) | 32路 Worker 正在极速消费中... | 进度: {npa:03d}-{nxx:03d}-{line:04d}", flush=True)

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
