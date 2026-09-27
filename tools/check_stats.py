# -*- coding: utf-8 -*-
"""
TPS 采集与入库全景执行大报表 (含执行次数、队列状态、过滤详情、MySQL手机号沉淀)
"""
import os
import sys
import time
from pathlib import Path
import redis

ROOT = Path(__file__).resolve().parent.parent

def load_env():
    for f in (ROOT / ".env", ROOT / "deploy" / ".env"):
        if f.exists():
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass

def get_stats():
    load_env()
    # 1. 查询 MySQL 数据库
    db_data = {"total": 0, "phones": 0, "wireless": 0, "err": None}
    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host=os.environ.get("TPS_DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_DB_PORT", 3306)),
            user=os.environ.get("TPS_DB_USER", "root"),
            password=os.environ.get("TPS_DB_PASSWORD", "tps123456"),
            database=os.environ.get("TPS_DB_NAME", "people_search"),
            connection_timeout=3
        )
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM persons WHERE person_id NOT LIKE 'http%' AND person_id NOT LIKE '%resultphone%'")
        db_data["total"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT person_id) FROM phone_numbers")
        db_data["phones"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM phone_numbers WHERE LOWER(line_type)='wireless'")
        db_data["wireless"] = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        db_data["err"] = str(e)

    # 2. 查询 Redis 队列与计数器
    redis_data = {
        "attempt": 0, "success": 0, "empty": 0, "retry": 0, "cf_fail": 0, "rate_limit": 0,
        "pending": 0, "processing": 0, "seen": 0, "err": None
    }
    try:
        r = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,
            socket_timeout=3
        )
        # 读取计数器
        redis_data["attempt"] = int(r.get("tps:metrics:counter:attempt") or 0)
        redis_data["success"] = int(r.get("tps:metrics:counter:success") or 0)
        redis_data["empty"] = int(r.get("tps:metrics:counter:empty") or 0)
        redis_data["retry"] = int(r.get("tps:metrics:counter:retry") or 0)
        redis_data["cf_fail"] = int(r.get("tps:metrics:counter:cf_fail") or 0)
        redis_data["rate_limit"] = int(r.get("tps:metrics:counter:rate_limit") or 0)

        # 读取队列深度
        redis_data["pending"] = int(r.llen("tps:pending") or 0)
        redis_data["processing"] = int(r.llen("tps:processing") or 0)
        redis_data["seen"] = int(r.scard("tps:seen") or 0)
    except Exception as e:
        redis_data["err"] = str(e)

    return db_data, redis_data

def main():
    db, r = get_stats()

    # 计算成功率
    attempts = r["attempt"]
    success = r["success"]
    empty = r["empty"]
    rate = f"{(success / attempts * 100):.1f}%" if attempts > 0 else "--"

    print("\n" + "=" * 65)
    print("        📊 TruePeopleSearch 32路任务全景执行大报表")
    print("=" * 65)

    print("\n【1. 任务流转与执行统计】")
    print(f"  • 累计执行总请求 (Attempts)   : {attempts:>10,} 次")
    print(f"  • 抓取成功并处理 (Success)    : {success:>10,} 次 (成功率: {rate})")
    print(f"  • 无电话无价值过滤 (Filtered) : {empty:>10,} 条 (已自动拦截丢弃)")
    print(f"  • 触发重试 / 限流保护 (Retry) : {r['retry'] + r['rate_limit']:>10,} 次 (自愈重试中)")

    print("\n【2. 实时任务队列状态 (Redis)】")
    print(f"  • 待抓取队列池 (Pending)      : {r['pending']:>10,} 条 (调度蓄水池)")
    print(f"  • 当前飞跃执行中 (Processing) : {r['processing']:>10,} 条 (32路并发运行)")
    print(f"  • URL 种子发现总量 (Seen)     : {r['seen']:>10,} 条 (持续向后挖掘)")

    print("\n【3. 最终数据库高价值资产 (MySQL)】")
    print(f"  • 沉淀有效人物档案库          : {db['total']:>10,} 条")
    print(f"  • 拥有联系电话的人物档案      : {db['phones']:>10,} 条 (100% 拥有联系方式)")
    print(f"  • 其中真实手机号(Wireless)    : {db['wireless']:>10,} 条 (高价值线索)")

    print("\n【4. 系统与安全模式】")
    print("  • 当前运行模式                : 32 路稳健安全档 (机器不卡、绝不黑屏)")
    if db["err"]:
        print(f"  • 数据库连接提示              : {db['err']}")
    if r["err"]:
        print(f"  • Redis 连接提示              : {r['err']}")
    print("=" * 65 + "\n")

if __name__ == "__main__":
    main()
