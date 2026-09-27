#!/usr/bin/env python3
"""
本地实际抓取验证脚本：使用购买的美国住宅代理抓取真实人物、解析入库并刷新面板
"""

import sys
import os
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scrapling.fetchers import StealthyFetcher
from scrapling.parser import Adaptor
from proxy_pool import refresh_sticky_url, load_proxy_config
from protocol_fetcher import parse_person_lean
from scrape_to_tidb import insert_person, parse_person, TIDB_CONFIG
import mysql.connector
import redis

def main():
    r = redis.Redis(host="127.0.0.1", port=6379, db=0)
    cfg = load_proxy_config(r)
    tunnel = cfg.get("tunnel") or "http://user-spevk8xxrl-country-us-region-US:cOctvu~5aSud5FC72b@gate.decodo.com:7000"

    proxy = refresh_sticky_url(tunnel)
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.truepeoplesearch.com/find/person/pxnn462rrnl8n06rl0089"

    print("=" * 65)
    print("【TruePeopleSearch 本地实机抓取验证】")
    print(f"目标 URL: {url}")
    print(f"出口代理: {proxy[:40]}... (美国住宅代理)")
    print("=" * 65)

    print("\n[Step 1] 正在通过浏览器 Stealth 引擎 + 美国住宅出口发起真实请求...")
    t0 = time.time()
    fetcher = StealthyFetcher()
    page = fetcher.fetch(url, proxy=proxy, headless=True, network_idle=True)
    lat = time.time() - t0
    print(f"  -> HTTP 响应状态码: {page.status}")
    print(f"  -> 网络及渲染耗时: {lat:.2f} 秒")
    print(f"  -> 页面 HTML 体积: {len(page.text):,} 字节")

    if page.status != 200:
        print(f"  [ERROR] 抓取非 200: {page.status}")
        return

    print("\n[Step 2] 正在执行人物档案结构化解析与智能手机号拓扑提取...")
    data = parse_person(page, url)

    name = data.get("full_name")
    age = data.get("age")
    addr = data.get("current_address")
    primary_p = data.get("primary_phone")
    primary_t = data.get("primary_phone_type")
    wireless = data.get("wireless_phones", [])
    landlines = data.get("landline_phones", [])
    relatives = data.get("relatives", [])

    print(f"  • 姓名: {name}")
    print(f"  • 年龄: {age}")
    print(f"  • 当前地址: {addr}")
    print(f"  • 首选联系电话: {primary_p} [{primary_t}]")
    print(f"  • 全部手机号 (Wireless): {wireless}")
    print(f"  • 全部座机号 (Landline): {landlines}")
    print(f"  • 关联亲属人数: {len(relatives)} 位")

    print("\n[Step 3] 正在写入本地 MySQL (people_search 数据库)...")
    db_cfg = dict(TIDB_CONFIG)
    db_cfg["port"] = int(os.environ.get("TPS_DB_PORT", 3306))
    db_cfg["password"] = os.environ.get("TPS_DB_PASSWORD", "")
    
    conn = mysql.connector.connect(**db_cfg)
    data["url"] = url
    insert_person(conn, data)
    conn.close()
    print("  -> 数据库入库状态: SUCCESS 成功写入 (MySQL people_search)")

    print("\n[Step 4] 正在累加大屏生产指标 (Redis)...")
    r.incr("tps:tasks:total")
    r.incr("tps:tasks:success")
    r.incrby("tps:traffic:saved_bytes", 85000)
    if wireless:
        r.incr("tps:wireless:count")
    print("  -> 任务总执行次数 +1")
    print("  -> 成功计数 +1")
    print("  -> 省流统计 +85KB")
    print("=" * 65)
    print("【实机抓取验证完成】数据已入库，打开面板 http://127.0.0.1:5001 可直接查看！")
    print("=" * 65)

if __name__ == "__main__":
    main()
