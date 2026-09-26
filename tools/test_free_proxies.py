#!/usr/bin/env python3
"""
全网公开免费代理实测脚本 (Free Proxy Reality Check)

目的：
真实测试公开免费代理（来自 GitHub / ProxyScrape 公开源）面对 TruePeopleSearch + Cloudflare 时的真实表现。
统计指标：
- 节点存活率 (能连通的比例)
- 响应延迟 (平均耗时)
- Cloudflare 拦截率 (403 / 503 / Turnstile 盾)
- 最终有效穿透率 (拿到真实人物数据的比例)
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import List

_ROOT = str(Path(__file__).resolve().parent.parent)
_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
for p in (_ROOT, _SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import httpx
from protocol_fetcher import (
    CloudflareChallengeError,
    EmptyPageError,
    FetchTimeoutError,
    HttpError,
    ProtocolFetcher,
    ProxyError,
)

TARGET_URL = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"
FREE_PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=yes&anonymity=all",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]


async def fetch_free_proxies(limit: int = 30) -> List[str]:
    """从全网公开开源仓库拉取一批免费代理"""
    proxies = []
    print("[1/3] 正在从全网免费开源代理源拉取最新候选 IP 列表...")
    async with httpx.AsyncClient(timeout=10) as client:
        for src in FREE_PROXY_SOURCES:
            try:
                resp = await client.get(src)
                if resp.status_code == 200:
                    lines = [
                        line.strip()
                        for line in resp.text.splitlines()
                        if line.strip() and ":" in line and not line.startswith("#")
                    ]
                    proxies.extend(lines)
                    print(f"  • 从 {src[:55]}... 获取到 {len(lines)} 个节点")
                    if len(proxies) >= limit * 2:
                        break
            except Exception as e:
                print(f"  • 拉取失败 ({src[:40]}...): {e}")

    # 去重并挑选前 limit 个
    unique_proxies = []
    seen = set()
    for p in proxies:
        p_norm = f"http://{p}" if not p.startswith("http") else p
        if p_norm not in seen:
            seen.add(p_norm)
            unique_proxies.append(p_norm)
        if len(unique_proxies) >= limit:
            break

    print(f"  -> 筛选出前 {len(unique_proxies)} 个免费候选代理进行实测\n")
    return unique_proxies


async def test_single_proxy(fetcher: ProtocolFetcher, proxy: str, idx: int, total: int) -> dict:
    """测试单个免费代理请求 TruePeopleSearch"""
    start_t = time.time()
    res = {
        "index": idx,
        "proxy": proxy,
        "alive": False,
        "cf_blocked": False,
        "success": False,
        "latency": 0.0,
        "status_desc": "",
    }

    print(f"[{idx:02d}/{total:02d}] 测试: {proxy:<28} ... ", end="", flush=True)

    try:
        data = await fetcher.fetch_person(TARGET_URL, proxy=proxy, timeout=8)
        cost = time.time() - start_t
        res["alive"] = True
        res["success"] = True
        res["latency"] = cost
        res["status_desc"] = f"穿透成功 (获取到: {data.get('full_name')})"
        print(f"\033[32m[200 PASS]\033[0m 耗时: {cost:.2f}s | {res['status_desc']}")

    except CloudflareChallengeError as e:
        cost = time.time() - start_t
        res["alive"] = True
        res["cf_blocked"] = True
        res["latency"] = cost
        res["status_desc"] = f"CF 拦截 (5秒盾/Turnstile/403)"
        print(f"\033[33m[CF BLOCKED]\033[0m 耗时: {cost:.2f}s | {res['status_desc']}")

    except (ProxyError, FetchTimeoutError) as e:
        cost = time.time() - start_t
        res["latency"] = cost
        res["status_desc"] = f"代理已死或连接超时 ({e})"
        print(f"\033[31m[DEAD / TIMEOUT]\033[0m 耗时: {cost:.2f}s")

    except HttpError as e:
        cost = time.time() - start_t
        res["alive"] = True
        res["latency"] = cost
        res["status_desc"] = f"HTTP {e.status}"
        print(f"\033[31m[HTTP {e.status}]\033[0m 耗时: {cost:.2f}s")

    except Exception as e:
        cost = time.time() - start_t
        res["latency"] = cost
        res["status_desc"] = f"未知异常: {e}"
        print(f"\033[31m[FAIL]\033[0m 耗时: {cost:.2f}s | {e}")

    return res


async def main():
    print("=" * 70)
    print("  全网公开免费开源代理池实战穿透率检测")
    print(f"  目标站点: {TARGET_URL}")
    print("=" * 70)

    test_count = 25
    proxies = await fetch_free_proxies(limit=test_count)
    if not proxies:
        print("[ERROR] 未获取到可用候选代理，请检查网络连接！")
        return

    print(f"[2/3] 正在使用 Chrome 124 TLS 协议层发起探测 (超时阈值: 8s)...")
    fetcher = ProtocolFetcher(default_timeout=8)

    # 限制并发测试，避免瞬间卡住
    tasks = []
    semaphore = asyncio.Semaphore(5)

    async def _sem_test(p, i):
        async with semaphore:
            return await test_single_proxy(fetcher, p, i, len(proxies))

    for idx, p in enumerate(proxies, 1):
        tasks.append(_sem_test(p, idx))

    results = await asyncio.gather(*tasks)

    # [3/3] 汇总统计报告
    print("\n" + "=" * 70)
    print("  [3/3] 全网公开免费代理实测总结报告")
    print("=" * 70)

    total = len(results)
    alive_count = sum(1 for r in results if r["alive"])
    cf_block_count = sum(1 for r in results if r["cf_blocked"])
    dead_count = total - alive_count
    success_count = sum(1 for r in results if r["success"])

    alive_lats = [r["latency"] for r in results if r["alive"]]
    avg_lat = sum(alive_lats) / len(alive_lats) if alive_lats else 0.0

    print(f"  • 总测试免费节点数: {total} 个")
    print(f"  • 物理死节点/超时数: {dead_count} 个 (\033[31m死亡率: {dead_count/total*100:.1f}%\033[0m)")
    print(f"  • 存活但被 CF 拦截: {cf_block_count} 个 (\033[33m拦截率: {cf_block_count/total*100:.1f}%\033[0m)")
    print(f"  • 真实穿透成功节点: \033[32m{success_count} 个\033[0m (\033[32m成功率: {success_count/total*100:.1f}%\033[0m)")
    if alive_lats:
        print(f"  • 存活节点平均响应: {avg_lat:.2f} 秒")
    print("=" * 70)

    if success_count == 0:
        print("\033[31m【测试结论】实测 0% 穿透率！公开免费代理全部死锁或被 Cloudflare 直接拦截。\033[0m")
        print("\033[31m事实证明：免费代理绝对无法支撑高并发爬虫业务，必须使用高质量动态住宅代理。\033[0m")
    else:
        print(f"【测试结论】成功率仅为 {success_count/total*100:.1f}%，无法支撑 3000万/天 生产要求。")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
