#!/usr/bin/env python3
"""
PoC：curl_cffi 粘性 IP + Cookie 复用时长测试
------------------------------------------------------------
用法：
    python tools/test_cookie_reuse_poc.py --session-id sticky_check --duration 1
    python tools/test_cookie_reuse_poc.py --url 'https://www.truepeoplesearch.com/find/person/...' --duration 5
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

from tps_poc_common import (  # noqa: E402
    assert_same_exit_ip,
    is_valid_person_response,
    make_curl_session,
    probe_exit_ip,
    sticky_label,
    sticky_proxy_url,
)

try:
    from curl_cffi import requests as c_requests  # noqa: F401
except ImportError:
    print("[ERROR] 缺少 curl_cffi 依赖，请运行: pip install curl-cffi")
    sys.exit(1)


# 旧样本 px82… 多轮 410；默认用队列较新 URL，也可用 --url 覆盖
DEFAULT_TEST_URLS = [
    "https://www.truepeoplesearch.com/find/person/b3yvmg99vfhh4lfm",
    "https://www.truepeoplesearch.com/find/person/rr17limpgdmn75o",
]


def parse_cookie_string(cookie_str: str) -> dict:
    cookies = {}
    if not cookie_str:
        return cookies
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _cookie_keys(session) -> list[str]:
    try:
        return list(session.cookies.get_dict().keys())
    except Exception:
        return []


def test_cookie_life(
    proxy_url: str,
    session_id: str,
    cookie_input: str,
    test_interval: int,
    total_duration_min: int,
    test_urls: list[str],
):
    final_proxy = sticky_proxy_url(proxy_url, session_id) if proxy_url and session_id else (proxy_url or "")

    print("=" * 70)
    print("【测试评估】curl_cffi 协议伪装 + IP锁定 + Cookie复用测试")
    print(f"原始代理: {proxy_url or '直连/默认'}")
    print(f"会话代理: {final_proxy or '直连/默认'}")
    print(f"会话 ID: {session_id}")
    print(f"目标 URL: {test_urls[0]}")
    print(f"注入 Cookie: {cookie_input[:40] + '...' if cookie_input else '无 (空凭证：测新会话能否自己开页)'}")
    print(f"测试间隔: 每 {test_interval} 秒 (附带 0.5-2.0s 随机扰动) | 总测试: {total_duration_min} 分钟")
    print("=" * 70)

    initial_referer = "https://www.google.com/"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "priority": "u=0, i",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "cross-site",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    session = make_curl_session(final_proxy)

    injected_cookies = parse_cookie_string(cookie_input)
    if injected_cookies:
        session.cookies.update(injected_cookies)
        print(f"[Cookie 注入] {len(injected_cookies)} 个: {list(injected_cookies.keys())}")
    else:
        print("[Cookie 注入] 未注入。本脚本测的是「当前会话能否开人物页」，不是空 Cookie 的复用寿命。")

    print("\n[IP 基线] 发包前连续探测两次…")
    ip_a = probe_exit_ip(session, label="IP#1", timeout=15)
    time.sleep(1.0)
    ip_b = probe_exit_ip(session, label="IP#2", timeout=15)
    sticky_status = assert_same_exit_ip(ip_a, ip_b, "发包前双探")
    if sticky_status == "drift":
        print("[中止] 粘性未锁住出口，不进入人物页测试。")
        _print_summary(0, 0, 0, False, 0.0, sticky_status)
        return
    if sticky_status == "unknown":
        print("[warning] 出口探测失败，粘性未确认；继续测人物页（仅确认漂移才硬中止）。")
    baseline_ip = ip_b or ip_a

    start_time = time.time()
    end_time = start_time + (total_duration_min * 60)
    request_count = 0
    success_count = 0
    blocked_count = 0
    cookie_names = set(injected_cookies) | set(_cookie_keys(session))
    had_valid_credential = "cf_clearance" in cookie_names or "cb_clearance" in cookie_names

    print("\n[Phase 1] 轨迹伪装发包 (Google -> 人物详情页)...")

    phase1_passed = False
    request_count += 1
    try:
        req_headers = headers.copy()
        req_headers["referer"] = initial_referer
        t0 = time.time()
        resp = session.get(test_urls[0], headers=req_headers, timeout=20)
        elapsed = time.time() - t0
        is_valid, reason = is_valid_person_response(resp.status_code, resp.text)
        print(
            f"--> [REQ #1] HTTP {resp.status_code} | 耗时: {elapsed:.2f}s | "
            f"字节: {len(resp.text)} | Cookie: {_cookie_keys(session)} | 判定: {reason}"
        )
        if is_valid:
            success_count += 1
            phase1_passed = True
            jar = session.cookies.get_dict()
            if "cf_clearance" in jar or "cb_clearance" in jar:
                had_valid_credential = True
            print("[PASSED] Phase 1 拿到人物页。")
        else:
            blocked_count += 1
            print(f"[FAILED] Phase 1 未通过: {reason}")
    except Exception as exc:
        blocked_count += 1
        print(f"[ERROR] 首次请求异常（已计入发包）: {exc}")

    if not phase1_passed:
        print("\n[中止] Phase 1 未通过，不进入 Phase 2。没有有效人物页，谈不上「凭证失效时间」。")
        _print_summary(request_count, success_count, blocked_count, had_valid_credential, 0.0, sticky_status)
        return

    print("\n[IP 复检] Phase 2 前…")
    ip_phase2 = probe_exit_ip(session, label="IP#phase2", timeout=15)
    phase2_sticky = assert_same_exit_ip(baseline_ip, ip_phase2, "Phase1→Phase2")
    if phase2_sticky == "drift":
        print("[中止] 进入轮询前出口已漂，Cookie 寿命结论无效。")
        _print_summary(request_count, success_count, blocked_count, had_valid_credential, 0.0, "drift")
        return
    if phase2_sticky == "ok":
        sticky_status = "ok"

    print(f"\n[Phase 2] 轮询复用 (每 {test_interval}s + 0.5-2.0s 抖动)...")
    index = 1
    while time.time() < end_time:
        time.sleep(test_interval + random.uniform(0.5, 2.0))
        target_url = test_urls[index % len(test_urls)]
        prev_url = test_urls[(index - 1) % len(test_urls)]
        index += 1
        curr_min = (time.time() - start_time) / 60.0

        req_headers = headers.copy()
        req_headers["referer"] = prev_url
        req_headers["sec-fetch-site"] = "same-origin"

        request_count += 1
        try:
            t0 = time.time()
            resp = session.get(target_url, headers=req_headers, timeout=20)
            req_time = time.time() - t0
            is_valid, reason = is_valid_person_response(resp.status_code, resp.text)
            if is_valid:
                success_count += 1
                print(
                    f"[{curr_min:5.1f}m] REQ #{request_count:03d} -> HTTP 200 | "
                    f"{req_time:.2f}s | {len(resp.text)}B | ✅ {reason}"
                )
            else:
                blocked_count += 1
                ip_fail = probe_exit_ip(session, label="IP#on_block", timeout=15)
                on_block = assert_same_exit_ip(baseline_ip, ip_fail, "被拦时")
                print(
                    f"[{curr_min:5.1f}m] REQ #{request_count:03d} -> HTTP {resp.status_code} | "
                    f"{req_time:.2f}s | ❌ {reason}"
                )
                if on_block == "drift":
                    sticky_status = "drift"
                    print("--> [漂移切入点] 出口变了，不能记成凭证失效")
                elif on_block == "ok" and had_valid_credential:
                    print(f"--> [失效切入点] 持有凭证约 {curr_min:.1f} 分钟后再次被拦（IP 未漂）")
                elif on_block == "ok":
                    print(f"--> [失败切入点] 无 clearance 凭证，约 {curr_min:.1f} 分钟后被拦")
                else:
                    print("--> [未确认] 被拦时 IP 探测失败，无法区分凭证失效与出口漂移")
                break
        except Exception as exc:
            blocked_count += 1
            print(f"[{curr_min:5.1f}m] REQ #{request_count:03d} -> 网络异常（已计入失败）: {exc}")
            break

    held_min = max(0.0, (time.time() - start_time) / 60.0) if success_count else 0.0
    _print_summary(request_count, success_count, blocked_count, had_valid_credential, held_min, sticky_status)


def _print_summary(request_count, success_count, blocked_count, had_valid_credential, held_min, sticky_status: str):
    print("\n" + "=" * 70)
    print("【测试总结】")
    print(f"• 总发包次数: {request_count}")
    print(f"• 成功次数: {success_count} | 被打回/失败: {blocked_count}")
    print(f"• 是否曾持有 clearance 类 Cookie: {'是' if had_valid_credential else '否'}")
    print(f"• 粘性 IP: {sticky_label(sticky_status)}")
    if success_count > 0 and sticky_status == "ok":
        print(f"• 观测窗口约 {held_min:.1f} 分钟内成功 {success_count} 次（以人物字段为准）")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Cookie 复用时长与 TLS 伪装测试脚本")
    parser.add_argument("--proxy", help="HTTP/HTTPS 代理 URL", default=os.environ.get("PROXY_TUNNEL") or os.environ.get("CLOUDBYPASS_PROXY"))
    parser.add_argument("--session-id", help="粘性 session 标记", default="test_cookie_poc")
    parser.add_argument("--cookie", help="注入 Cookie 字符串 (k=v; ...)", default="")
    parser.add_argument("--url", help="人物页 URL（可重复；默认用队列样本）", action="append", default=None)
    parser.add_argument("--interval", type=int, help="发包间隔(秒)", default=15)
    parser.add_argument("--duration", type=int, help="测试总时长(分钟)", default=20)
    args = parser.parse_args()
    urls = args.url if args.url else list(DEFAULT_TEST_URLS)
    test_cookie_life(args.proxy, args.session_id, args.cookie, args.interval, args.duration, urls)


if __name__ == "__main__":
    main()
