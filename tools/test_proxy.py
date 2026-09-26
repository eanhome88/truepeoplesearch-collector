#!/usr/bin/env python3
"""
IP 代理池与 Cloudflare 协议层穿透能力联调测试工具

用于在正式启动大规模抓取前，验证购买的代理（隧道、API、本地列表）是否能成功穿透 TruePeopleSearch 的 Cloudflare 防护。

用法示例：
  # 1. 测试单条代理 / 隧道代理
  python3 tools/test_proxy.py --proxy "http://username:password@gateway:port"

  # 2. 测试本地代理文件中的前 10 个 IP
  python3 tools/test_proxy.py --proxy-file proxies.txt --count 10

  # 3. 测试 API 动态提取的代理
  python3 tools/test_proxy.py --proxy-api "http://api.proxy.com/get" --count 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
for p in (_ROOT, _SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

from protocol_fetcher import (
    CloudflareChallengeError,
    EmptyPageError,
    FetchTimeoutError,
    HttpError,
    ProtocolFetcher,
    ProxyError,
)
from proxy_pool import ProxyManager

DEFAULT_TEST_URL = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"


async def test_single_request(
    fetcher: ProtocolFetcher,
    url: str,
    proxy_url: str | None,
    index: int,
    total: int,
) -> dict:
    """测试单个代理请求"""
    masked = ProxyManager._mask_proxy(proxy_url) if proxy_url else "Direct (直连)"
    start_time = time.time()
    result = {
        "index": index,
        "proxy": masked,
        "status": 0,
        "latency_s": 0.0,
        "success": False,
        "cf_blocked": False,
        "detail": "",
        "name": "",
    }

    print(f"[{index}/{total}] 正在请求 -> 代理: {masked} ...", end="", flush=True)

    try:
        data = await fetcher.fetch_person(url, proxy=proxy_url, timeout=12)
        latency = time.time() - start_time
        result["status"] = 200
        result["latency_s"] = latency
        result["success"] = True
        result["name"] = data.get("full_name") or ""
        result["detail"] = f"解析成功 (姓名: {result['name']}, 地址: {data.get('current_city')})"
        print(f" \033[32m[PASS]\033[0m 耗时: {latency:.2f}s | {result['detail']}")

    except CloudflareChallengeError as e:
        latency = time.time() - start_time
        result["latency_s"] = latency
        result["cf_blocked"] = True
        result["detail"] = f"Cloudflare 拦截 (5秒盾/Turnstile/403/503): {e}"
        print(f" \033[31m[CF_BLOCKED]\033[0m 耗时: {latency:.2f}s | {e}")

    except EmptyPageError as e:
        latency = time.time() - start_time
        result["status"] = 404
        result["latency_s"] = latency
        result["success"] = True  # 页面正常响应，只是没有数据
        result["detail"] = f"页面为空或404 (无数据): {e}"
        print(f" \033[33m[EMPTY]\033[0m 耗时: {latency:.2f}s | {e}")

    except (ProxyError, FetchTimeoutError) as e:
        latency = time.time() - start_time
        result["latency_s"] = latency
        result["detail"] = f"代理握手/连接超时: {e}"
        print(f" \033[31m[TIMEOUT/PROXY_ERR]\033[0m 耗时: {latency:.2f}s | {e}")

    except Exception as e:
        latency = time.time() - start_time
        result["latency_s"] = latency
        result["detail"] = f"异常: {e}"
        print(f" \033[31m[ERROR]\033[0m 耗时: {latency:.2f}s | {e}")

    return result


async def main_async():
    parser = argparse.ArgumentParser(description="测试代理池对 TruePeopleSearch 的协议穿透能力")
    parser.add_argument("--proxy", help="单个代理连接串 (http://user:pass@host:port)")
    parser.add_argument("--proxy-file", help="本地代理文件路径 (每行一个代理)")
    parser.add_argument("--proxy-api", help="代理动态提取 API 地址")
    parser.add_argument("--url", default=DEFAULT_TEST_URL, help="测试用的人物 URL")
    parser.add_argument("--count", type=int, default=5, help="测试请求次数 (默认 5)")
    args = parser.parse_args()

    print("=" * 65)
    print("TruePeopleSearch 协议层 + IP 代理池穿透能力联调测试")
    print(f"测试目标 URL: {args.url}")
    print(f"计划请求次数: {args.count}")
    print("=" * 65)

    proxy_mgr = ProxyManager(
        tunnel=args.proxy,
        proxy_file=args.proxy_file,
        api_url=args.proxy_api,
    )
    await proxy_mgr.start_background_tasks()

    fetcher = ProtocolFetcher()
    results = []

    try:
        for i in range(1, args.count + 1):
            proxy = await proxy_mgr.get_proxy()
            res = await test_single_request(fetcher, args.url, proxy, i, args.count)
            results.append(res)
            if i < args.count:
                await asyncio.sleep(0.5)
    finally:
        await proxy_mgr.stop_background_tasks()

    # 统计汇总
    total = len(results)
    success_count = sum(1 for r in results if r["success"])
    cf_block_count = sum(1 for r in results if r["cf_blocked"])
    other_err_count = total - success_count - cf_block_count
    latencies = [r["latency_s"] for r in results if r["latency_s"] > 0]
    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0

    print("\n" + "=" * 65)
    print("联调测试总结报告:")
    print(f"  • 总测试次数: {total}")
    print(f"  • 穿透成功率: {success_count}/{total} ({(success_count/total*100) if total else 0:.1f}%)")
    print(f"  • CF 盾拦截数: {cf_block_count}/{total} ({(cf_block_count/total*100) if total else 0:.1f}%)")
    print(f"  • 超时/其他错误: {other_err_count}/{total}")
    print(f"  • 平均网络耗时: {avg_lat:.2f} 秒")
    print("=" * 65)

    if success_count / total >= 0.8:
        print("\033[32m[结论] 代理池质量优异！协议层穿透率极佳，可直接启动单机 300 万高并发抓取！\033[0m")
    elif cf_block_count > 0:
        print("\033[33m[建议] 存在部分 Cloudflare 拦截。请优先选用高质量住宅代理（Residential Proxy），或更换出口地区。\033[0m")
    else:
        print("\033[31m[建议] 大部分请求超时或代理连接失败，请检查代理用户名密码、白名单设置或网络配置。\033[0m")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
