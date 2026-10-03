#!/usr/bin/env python3
"""
测试 Cloudbypass (穿云) 代理与 API 连通性
包含两种模式：
1. 传统隧道代理模式 (Forward Proxy via gw-res.cloudbypass.com:1288)
2. 穿云 API 模式 (Cloudflare Bypass API via api.cloudbypass.com)
"""

import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from cloudbypass_v2 import fetch_sync

PROXY_TUNNEL = os.environ.get("CLOUDBYPASS_PROXY") or os.environ.get("PROXY_TUNNEL") or ""
TEST_TARGET = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60"


def test_mode_1_forward_proxy():
    print("\n" + "=" * 60)
    print("【模式 1 测试】直接走 HTTP 动态住宅代理 (Forward Proxy)")
    print("代理地址: 来自 CLOUDBYPASS_PROXY 或 PROXY_TUNNEL")
    print("=" * 60)
    
    # 1. 先测 IP 出口与国家
    try:
        t0 = time.time()
        with httpx.Client(proxy=PROXY_TUNNEL, timeout=15) as client:
            resp = client.get("http://httpbin.org/ip")
            print(f"[IP测试] 耗时: {time.time()-t0:.2f}s, 返回: {resp.text.strip()}")
    except Exception as e:
        print(f"[IP测试失败] {e}")

    # 2. 测 TruePeopleSearch 目标站
    try:
        t0 = time.time()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        with httpx.Client(proxy=PROXY_TUNNEL, timeout=20, headers=headers) as client:
            resp = client.get(TEST_TARGET)
            print(f"[目标站测试] HTTP 状态码: {resp.status_code}, 耗时: {time.time()-t0:.2f}s, 长度: {len(resp.text)}")
            if resp.status_code == 200:
                print(f"[成功] 页面前200字符: {resp.text[:200]!r}")
            elif resp.status_code == 403:
                print("[拦截] 遇到 403 (可能是 Cloudflare WAF/盾)")
    except Exception as e:
        print(f"[目标站测试失败] {e}")


def test_mode_2_cloudbypass_api():
    print("\n" + "=" * 60)
    print("【模式 2 测试】穿云 v2 Cookie 模式 (默认指纹，不启用分区)")
    print("=" * 60)
    if not os.environ.get("CLOUDBYPASS_APIKEY") or not PROXY_TUNNEL:
        print("[跳过] 需要环境变量 CLOUDBYPASS_APIKEY 和 CLOUDBYPASS_PROXY")
        return

    try:
        t0 = time.time()
        page = fetch_sync(TEST_TARGET, proxy=PROXY_TUNNEL, timeout=60, max_retries=2)
        elapsed = time.time() - t0
        if page is None:
            print(f"[穿云v2] 未通过，耗时 {elapsed:.2f}s。响应头 x-cb-status 不是 ok，或返回了挑战页")
            return
        print(
            f"[穿云v2] HTTP {page.status}，x-cb-status={page.cb_status}，"
            f"耗时 {elapsed:.2f}s，长度 {len(page.body)}"
        )
        print(f"[穿云v2] 页面前200字符: {page.body[:200]!r}")
    except Exception as e:
        print(f"[穿云API测试失败] {e}")


def test_mode_3_curl_cffi_with_proxy():
    print("\n" + "=" * 60)
    print("【模式 3 测试】curl_cffi 模拟 Chrome124 + 穿云住宅代理")
    print("=" * 60)
    try:
        from curl_cffi import requests as c_requests
        t0 = time.time()
        proxies = {"http": PROXY_TUNNEL, "https": PROXY_TUNNEL}
        resp = c_requests.get(
            TEST_TARGET,
            proxies=proxies,
            impersonate="chrome124",
            timeout=25
        )
        print(f"[curl_cffi测试] HTTP 状态码: {resp.status_code}, 耗时: {time.time()-t0:.2f}s, 长度: {len(resp.text)}")
        if resp.status_code == 200:
            print(f"[curl_cffi大成功!] 成功穿透 Cloudflare，页面长度: {len(resp.text)}")
            print(f"页面预览: {resp.text[:300]!r}")
        elif resp.status_code == 403:
            print("[curl_cffi拦截] 403 页面拦截")
        elif resp.status_code == 429:
            print("[curl_cffi被限流] 429 Too Many Requests")
        else:
            print(f"[curl_cffi返回] 状态: {resp.status_code}")
    except Exception as e:
        print(f"[curl_cffi测试失败] {e}")


if __name__ == "__main__":
    test_mode_1_forward_proxy()
    test_mode_2_cloudbypass_api()
    test_mode_3_curl_cffi_with_proxy()
