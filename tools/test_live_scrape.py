#!/usr/bin/env python3
"""单页采集诊断。默认只解析；显式 --write 才会写入配置的数据库。"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import redis
from scrapling.fetchers import StealthyFetcher

from proxy_pool import load_proxy_config, refresh_sticky_url
from scrape_to_tidb import (
    _page_document, _page_final_url, fetch_cloudbypass_v2_sync, get_db,
    has_usable_phone, insert_person, is_captcha_document, parse_person,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="单页采集和可选入库诊断")
    parser.add_argument("url", help="经授权可访问的人物页 URL")
    parser.add_argument("--write", action="store_true", help="显式写入当前配置的数据库")
    args = parser.parse_args()

    r = redis.Redis(
        host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
    )
    try:
        tunnel = (load_proxy_config(r) or {}).get("tunnel")
    except redis.RedisError:
        tunnel = None
    tunnel = tunnel or os.environ.get("TPS_PROXY_URL") or os.environ.get("PROXY_TUNNEL")
    if not tunnel:
        print("[ERROR] 未配置代理；请设置 TPS_PROXY_URL、PROXY_TUNNEL 或 Redis 代理配置")
        return 2
    proxy = refresh_sticky_url(tunnel)

    print("[Step 1] 抓取单页（优先使用穿云 V2 API 网关）")
    started = time.time()
    page = None
    cb_tried = False
    if os.environ.get("USE_CLOUDBYPASS", "1") == "1":
        cb_tried = True
        try:
            page = fetch_cloudbypass_v2_sync(args.url, proxy=tunnel)
            if page is None:
                print("[CLOUDBYPASS] 穿云 API 返回验证码/空页，3 次重试均未突破")
        except Exception as e:
            print(f"[CLOUDBYPASS] API 抓取跳过: {e}")
    if page is None:
        if cb_tried:
            print("[FALLBACK] 穿云失败，降级到 StealthyFetcher 浏览器渲染...")
        page = StealthyFetcher.fetch(args.url, proxy=proxy, headless=True, network_idle=True)
    elapsed = time.time() - started
    doc = _page_document(page)
    # 诊断：输出页面基本信息
    import re as _re
    _titles = _re.findall(r"<title>(.*?)</title>", doc[:2000], _re.I)
    print(f"HTTP 状态: {page.status}，耗时: {elapsed:.2f} 秒，页面大小: {len(doc)} 字节")
    if _titles:
        print(f"页面标题: {_titles[0][:80]}")
    if page.status != 200:
        print("[ERROR] 非成功响应，未解析、未写库")
        return 1
    final_url = _page_final_url(page, args.url)
    if is_captcha_document(final_url, doc):
        # 找出具体命中了哪个标记
        blob = f"{final_url}\n{doc}".lower()
        from scrape_to_tidb import _CAPTCHA_MARKERS
        hits = [m for m in _CAPTCHA_MARKERS if m in blob]
        print(f"[BLOCKED] 页面要求验证或限制访问；未解析、未写库")
        print(f"  命中标记: {hits}")
        print(f"  页面前 300 字符: {doc[:300]!r}")
        return 1

    print("[Step 2] 解析并检查数据质量")
    data = parse_person(page, args.url)
    if not data.get("person_id") or not data.get("full_name"):
        print("[ERROR] 缺少人物标识或姓名，未写库")
        return 1
    if not has_usable_phone(data):
        print("[SKIPPED] 未解析到有效电话号码；未注入模拟数据，未写库")
        return 1
    print(f"解析成功：电话记录 {len(data.get('phone_numbers') or [])} 条")

    if not args.write:
        print("[DRY_RUN] 未写库。需要验证入库时显式加 --write。")
        return 0

    print("[Step 3] 写入当前配置的数据库")
    db = get_db()
    try:
        persisted = insert_person(db, data)
    finally:
        db.close()
    if not persisted:
        print("[SKIPPED] 数据未写入；不计为成功")
        return 1

    # 临时诊断脚本不写生产任务指标，防止把测试次数伪装成真实采集。
    print("[OK] 数据库已提交或确认同一记录已存在；未修改生产采集计数")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
