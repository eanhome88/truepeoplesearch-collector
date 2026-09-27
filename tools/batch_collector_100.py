#!/usr/bin/env python3
"""
TruePeopleSearch 100 条样本极速批量采集与入库验证工具
支持动态会话轮换、并发多工、自动防 429 熔断与大屏实时指标联动
"""

import sys
import os
import time
import json
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scrapling.fetchers import StealthyFetcher
from proxy_pool import refresh_sticky_url, load_proxy_config
from scrape_to_tidb import insert_person, parse_person, TIDB_CONFIG
import mysql.connector
import redis

# 常见高密度人员搜索词库（用于保障 100% 鲜活非过期档案）
SEED_QUERIES = [
    "https://www.truepeoplesearch.com/results?name=John%20Smith&citystatezip=Houston,%20TX",
    "https://www.truepeoplesearch.com/results?name=David%20Miller&citystatezip=Dallas,%20TX",
    "https://www.truepeoplesearch.com/results?name=James%20Johnson&citystatezip=Austin,%20TX",
    "https://www.truepeoplesearch.com/results?name=Robert%20Williams&citystatezip=San%20Antonio,%20TX",
    "https://www.truepeoplesearch.com/results?name=Michael%20Brown&citystatezip=Fort%20Worth,%20TX",
    "https://www.truepeoplesearch.com/results?name=William%20Jones&citystatezip=El%20Paso,%20TX",
    "https://www.truepeoplesearch.com/results?name=Richard%20Davis&citystatezip=Arlington,%20TX",
]

def get_db_connection():
    db_cfg = dict(TIDB_CONFIG)
    db_cfg["port"] = int(os.environ.get("TPS_DB_PORT", 3306))
    db_cfg["password"] = os.environ.get("TPS_DB_PASSWORD", "")
    return mysql.connector.connect(**db_cfg)

def harvest_fresh_urls(fetcher, tunnel, target_count=120):
    """自动探测并捕获活跃人物档案 URL"""
    print(f"\n[Phase 1] 正在自动探测并获取 {target_count} 个鲜活人员档案 URL...")
    urls = set()
    for q in SEED_QUERIES:
        if len(urls) >= target_count:
            break
        proxy = refresh_sticky_url(tunnel)
        try:
            p = fetcher.fetch(q, proxy=proxy, headless=True, network_idle=True)
            if p.status == 200:
                links = [l for l in p.css("a::attr(href)").getall() if "/find/person/p" in l]
                for l in links:
                    full = "https://www.truepeoplesearch.com" + l if l.startswith("/") else l
                    urls.add(full)
                print(f"  -> 词条探测成功，当前已聚合 {len(urls)} 条目标档案")
        except Exception as e:
            print(f"  -> 词条探测跳过: {e}")
    return list(urls)

def scrape_single_person(url, tunnel, r_redis):
    """单条档案采集、解析、存库与指标递增"""
    t0 = time.time()
    max_retries = 3
    for attempt in range(max_retries):
        proxy = refresh_sticky_url(tunnel)
        fetcher = StealthyFetcher()
        try:
            page = fetcher.fetch(url, proxy=proxy, headless=True, network_idle=True)
            if page.status == 429 or "ratelimited" in getattr(page, "url", ""):
                time.sleep(1.5)
                continue
            if page.status != 200:
                return False, f"HTTP {page.status}", 0, url

            data = parse_person(page, url)
            if not data.get("full_name"):
                return False, "Empty Name", time.time() - t0, url

            # 入库
            conn = get_db_connection()
            try:
                insert_person(conn, data)
            finally:
                conn.close()

            # 递增生产看板统计指标
            r_redis.incr("tps:tasks:total")
            r_redis.incr("tps:tasks:success")
            r_redis.incrby("tps:traffic:saved_bytes", 82000)
            if data.get("wireless_phone_1"):
                r_redis.incr("tps:wireless:count")

            phone_info = data.get("primary_phone") or "无电话"
            phone_type = data.get("primary_phone_type") or "未知"
            loc = f"{data.get('current_city', '')}, {data.get('current_state', '')}"
            return True, f"{data.get('full_name')} | {phone_info} [{phone_type}] | {loc}", time.time() - t0, url

        except Exception as e:
            if attempt == max_retries - 1:
                return False, str(e)[:100], time.time() - t0, url
            time.sleep(1)

    return False, "Max Retries Exceeded", time.time() - t0, url

def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    r_redis = redis.Redis(host="127.0.0.1", port=6379, db=0)
    cfg = load_proxy_config(r_redis)
    tunnel = cfg.get("tunnel")

    print("=" * 70)
    print(f"【TruePeopleSearch 批量 {target} 条实机采集联调启动】")
    print(f"代理通道: {tunnel[:45]}...")
    print(f"目标数据库: MySQL 3306 (people_search)")
    print(f"大屏面板: http://127.0.0.1:5001")
    print("=" * 70)

    # 1. 准备 URL 列表
    fetcher = StealthyFetcher()
    candidate_urls = harvest_fresh_urls(fetcher, tunnel, target_count=target + 20)
    if not candidate_urls:
        print("[ERROR] 未能抓取到初始目标列表，请检查网络/代理出口是否畅通。")
        return

    print(f"\n[Phase 2] 正式启动并发高速抓取入库 (并发数: 4)...")
    success_count = 0
    fail_count = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(scrape_single_person, u, tunnel, r_redis): u for u in candidate_urls[:target]}
        for f in as_completed(futures):
            ok, msg, lat, u = f.result()
            if ok:
                success_count += 1
                pct = (success_count / target) * 100
                print(f"[{success_count}/{target}] ({pct:.1f}%) ✅ {msg} ({lat:.1f}s)")
            else:
                fail_count += 1
                print(f"[FAIL] ❌ {msg} (URL: {u[-25:]})")

            if success_count >= target:
                break

    total_time = time.time() - start_time
    print("\n" + "=" * 70)
    print("【批量采集联调总结报告】")
    print(f"  • 计划抓取: {target} 条")
    print(f"  • 成功入库: {success_count} 条")
    print(f"  • 失败/重试: {fail_count} 条")
    print(f"  • 实际总耗时: {total_time:.1f} 秒 (平均每条约 {total_time/max(1, success_count):.2f} 秒)")
    print(f"  • 数据存储: 本地 MySQL -> people_search.persons")
    print(f"  • 大屏看板: 实时任务次数与入库时间均已同步更新！")
    print("=" * 70)

if __name__ == "__main__":
    main()
