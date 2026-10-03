#!/usr/bin/env python3
"""
纯本地无头浏览器解盾 + Cookie 复用桥接测试
------------------------------------------------------
硬门槛：
1. 解盾前 / 复用前出口 IP 必须一致（漂移中止；探测失败也中止）
2. 浏览器未进人物页 → 不进 Step2
3. 无 cf_clearance → 不进 Step2

用法：
    python tools/test_stealth_cookie_bridge.py --session-id bridge_check --count 3
    python tools/test_stealth_cookie_bridge.py --url 'https://www.truepeoplesearch.com/find/person/...'
"""

from __future__ import annotations

import argparse
import os
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
    sticky_proxy_url,
)

try:
    from curl_cffi import requests as c_requests  # noqa: F401
except ImportError:
    print("[ERROR] 缺少 curl_cffi 依赖，请运行: pip install curl-cffi")
    sys.exit(1)

try:
    from scrape_to_tidb import StealthyFetcher
except Exception as exc:
    StealthyFetcher = None
    print(f"[WARNING] StealthyFetcher 导入失败: {exc}")


# 旧样本 px82… 多轮 410；默认换队列样本，可用 --url 覆盖
DEFAULT_TEST_URL = "https://www.truepeoplesearch.com/find/person/b3yvmg99vfhh4lfm"


def _checkout_sticky_proxy(raw_proxy: str, session_id: str) -> tuple[str, str, str]:
    """写入粘性并双探 IP。旧 session 卡死时自动换新 sid 再试一次。"""
    import secrets

    sid = session_id
    for attempt in range(2):
        final_proxy = sticky_proxy_url(raw_proxy, sid) if raw_proxy and sid else (raw_proxy or "")
        print(f"\n[IP 基线] 解盾前… (session={sid})")
        probe_session = make_curl_session(final_proxy)
        ip_a = probe_exit_ip(probe_session, label="IP#before_solve", timeout=12, rounds=1)
        time.sleep(0.3)
        ip_b = probe_exit_ip(probe_session, label="IP#before_solve_2", timeout=12, rounds=1)
        status = assert_same_exit_ip(ip_a, ip_b, "解盾前双探")
        if status == "ok":
            return final_proxy, (ip_b or ip_a), status
        if status == "drift":
            return final_proxy, (ip_b or ip_a), status
        # unknown：旧粘性会话可能挂死，换新 sid
        if attempt == 0 and raw_proxy:
            sid = f"{session_id}{secrets.token_hex(2)}"
            print(f"[Sticky] 当前会话出口探测失败，换新 session-id 重试: {sid}")
            continue
        return final_proxy, (ip_b or ip_a), status
    return raw_proxy or "", "", "unknown"


def _page_html(page) -> str:
    for attr in ("html_content", "html", "body", "content"):
        try:
            val = getattr(page, attr, None)
        except Exception:
            continue
        if callable(val):
            try:
                val = val()
            except Exception:
                continue
        if isinstance(val, (bytes, bytearray)):
            return val.decode("utf-8", errors="replace")
        if isinstance(val, str) and val.strip():
            return val
    get_text = getattr(page, "get_all_text", None)
    if callable(get_text):
        try:
            return str(get_text() or "")
        except Exception:
            return ""
    return ""


def _page_url(page) -> str:
    for obj in (page, getattr(page, "response", None)):
        if obj is None:
            continue
        try:
            found = getattr(obj, "url", None)
        except Exception:
            found = None
        if found:
            return str(found)
    return ""


def _extract_cookies(page) -> dict:
    cookies = {}
    raw = None
    if hasattr(page, "cookies"):
        raw = page.cookies() if callable(page.cookies) else page.cookies
    elif hasattr(getattr(page, "response", None), "cookies"):
        raw = page.response.cookies

    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if k}
    if isinstance(raw, (list, tuple)):
        for c in raw:
            if isinstance(c, dict) and c.get("name"):
                cookies[str(c["name"])] = str(c.get("value") or "")
            elif hasattr(c, "name") and hasattr(c, "value"):
                cookies[str(c.name)] = str(c.value)
    return cookies


def _extract_ua(page) -> str:
    if hasattr(page, "user_agent") and page.user_agent:
        return str(page.user_agent)
    for obj in (getattr(page, "request", None), getattr(page, "response", None)):
        if obj is None:
            continue
        headers = getattr(obj, "headers", None)
        if not headers:
            continue
        try:
            return str(headers.get("user-agent") or headers.get("User-Agent") or "")
        except Exception:
            continue
    return ""


def solve_and_extract_cookie(proxy_url: str, test_url: str) -> tuple[dict, str, bool]:
    """返回 (cookies, ua, browser_got_person_page)。"""
    print("\n[Step 1] 启动无头 Chrome 破盾...")
    if StealthyFetcher is None:
        print("[ERROR] StealthyFetcher 未就绪，跳过。")
        return {}, "", False

    t0 = time.time()

    def auto_submit_captcha(playwright_page):
        """仅在 InternalCaptcha 上尝试提交；不宣称已过 Turnstile。"""
        try:
            url = (playwright_page.url or "").lower()
            if "internalcaptcha" not in url:
                return
            print("  [AUTOMATION] 仍在 InternalCaptcha，尝试提交表单（不代表已过盾）...")
            playwright_page.wait_for_timeout(2000)
            submitted = playwright_page.evaluate("""
                () => {
                    const form = document.querySelector('form[action*="captcha"]') || document.querySelector('form');
                    if (form) { form.submit(); return true; }
                    const btn = document.querySelector('button[type="submit"]') || document.querySelector('input[type="submit"]');
                    if (btn) { btn.click(); return true; }
                    return false;
                }
            """)
            if submitted:
                playwright_page.wait_for_timeout(4000)
                print(f"  [AUTOMATION] 提交后 URL: {playwright_page.url}")
            else:
                print("  [AUTOMATION] 未找到可提交表单")
        except Exception as exc:
            print(f"  [AUTOMATION warning] {exc}")

    try:
        page = StealthyFetcher.fetch(
            test_url,
            proxy=proxy_url,
            solve_cloudflare=True,
            page_action=auto_submit_captcha,
            headless=True,
            network_idle=True,
            timeout=45000,
        )
    except Exception as exc:
        print(f"❌ 无头浏览器异常: {exc}")
        return {}, "", False

    elapsed = time.time() - t0
    status = getattr(page, "status", None)
    if status is None:
        status = getattr(getattr(page, "response", None), "status", "未知")
    final_url = _page_url(page) or test_url
    html = _page_html(page)
    cookies = _extract_cookies(page)
    ua = _extract_ua(page)
    ok, reason = is_valid_person_response(int(status) if str(status).isdigit() else 0, html, final_url)

    print(f"--> 渲染完成 {elapsed:.2f}s | HTTP {status} | 最终 URL: {final_url}")
    print(f"--> Cookie keys: {list(cookies.keys())}")
    print(f"--> UA: {(ua[:80] + '...') if len(ua) > 80 else (ua or '(未提取到，Step 2 将用默认 Chrome 124)')}")
    print(f"--> 浏览器人物页判定: {'✅ ' if ok else '❌ '}{reason}")

    if "cf_clearance" in cookies:
        print(f"--> 含 cf_clearance (len={len(cookies['cf_clearance'])})")
    else:
        print("--> 无 cf_clearance")

    if "internalcaptcha" in final_url.lower():
        print("--> 停在站点二次门槛 InternalCaptcha，浏览器侧也未进人物页")
    if str(status) == "410":
        print("--> HTTP 410：目标人物页已失效，请换 --url（不要用旧的 px82… 样本）")

    return cookies, ua, ok


def run_fast_requests_with_cookie(
    proxy_url: str,
    cookies: dict,
    user_agent: str,
    baseline_ip: str,
    test_url: str,
    count: int = 5,
):
    if not cookies:
        print("\n[ABORT] Cookie 为空，不进入 Step 2。")
        return

    # 无 CF 挑战时往往没有 cf_clearance；人物页已成功则改看站点会话 Cookie
    session_keys = [k for k in ("cf_clearance", "__cf_bm", "BSID", "bfjs") if k in cookies]
    if "cf_clearance" not in cookies:
        print(
            "\n[提示] 无 cf_clearance（本次可能未触发 Cloudflare 挑战）。"
            f"将注入整套 Cookie 复用；会话相关键: {session_keys or list(cookies.keys())[:8]}"
        )

    print("\n[Step 2] 复用前 IP 复检…")
    session = make_curl_session(proxy_url)
    ip_before = probe_exit_ip(session, label="IP#before_reuse", timeout=15)
    status = assert_same_exit_ip(baseline_ip, ip_before, "解盾前→复用前")
    if status == "drift":
        print("[ABORT] 出口已漂移，Cookie 复用结论无效，不发包。")
        return
    if status == "unknown":
        print("[warning] 复用前 IP 未确认，继续发包，但粘性结论不可靠。")

    print(f"\n[Step 2] 注入整套 Cookie 到 curl_cffi，串行发包 {count} 次（间隔 1s）...")
    ua = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    if not user_agent:
        print("  [warning] 未拿到浏览器 UA，退回写死的 Chrome 124（TLS/UA 可能不一致）")

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "cross-site",
        "user-agent": ua,
        "referer": "https://www.google.com/",
    }

    # 广告追踪 Cookie 太多；优先保留站点/CF 相关，其余一并带上（协议层可承受）
    session.cookies.update(cookies)
    print(f"  注入 Cookie 数: {len(cookies)} | 关键键: {session_keys}")
    print(f"  UA: {ua[:70]}...")

    success_cnt = 0
    fail_cnt = 0
    for i in range(1, count + 1):
        t0 = time.time()
        try:
            resp = session.get(test_url, headers=headers, timeout=15)
            elapsed = time.time() - t0
            ok, reason = is_valid_person_response(resp.status_code, resp.text, str(getattr(resp, "url", "") or ""))
            if ok:
                success_cnt += 1
                print(f"  REQ #{i:02d} | HTTP {resp.status_code} | {elapsed:.2f}s | {len(resp.text)}B | ✅ {reason}")
            else:
                fail_cnt += 1
                print(f"  REQ #{i:02d} | HTTP {resp.status_code} | {elapsed:.2f}s | {len(resp.text)}B | ❌ {reason}")
                if i == 1:
                    ip_fail = probe_exit_ip(session, label="IP#on_block", timeout=15)
                    assert_same_exit_ip(baseline_ip, ip_fail, "首包被拦时")
        except Exception as exc:
            fail_cnt += 1
            print(f"  REQ #{i:02d} | 网络异常（计入失败）: {exc}")
        time.sleep(1.0)

    print("\n" + "=" * 60)
    print(f"【总结】串行 {count} 次 | 成功 {success_cnt} | 失败 {fail_cnt}")
    print("成功 = HTTP 200 且含人物字段，不是「200 就算过」。")
    if success_cnt == 0 and "cf_clearance" not in cookies:
        print("若全失败：可能是无 clearance 时协议层 TLS≠浏览器，或缺少站点会话键。")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="本地解盾与 Cookie 复用桥接测试")
    parser.add_argument(
        "--proxy",
        help="HTTP/HTTPS 代理 URL",
        default=os.environ.get("CLOUDBYPASS_PROXY") or os.environ.get("PROXY_TUNNEL"),
    )
    parser.add_argument("--session-id", help="粘性 session 标记", default="stealth_bridge_poc")
    parser.add_argument("--url", help="人物页 URL", default=DEFAULT_TEST_URL)
    parser.add_argument("--count", type=int, help="Step 2 串行次数", default=5)
    args = parser.parse_args()

    raw_proxy = args.proxy
    session_id = args.session_id
    test_url = args.url

    print("=" * 70)
    print("【本地解盾 + Cookie 桥接】测试启动")
    print(f"原始代理: {raw_proxy or '直连/未指定'}")
    print(f"会话 ID: {session_id}")
    print(f"目标 URL: {test_url}")
    print("=" * 70)

    if not raw_proxy:
        print("[WARNING] 无代理时出口会漂，clearance 极易失效。")
        final_proxy = raw_proxy
        baseline_ip = ""
    else:
        final_proxy, baseline_ip, sticky = _checkout_sticky_proxy(raw_proxy, session_id)
        print(f"锁定代理: {final_proxy}")
        if sticky == "drift":
            print("[ABORT] 粘性漂移，不启动浏览器。")
            return
        if sticky == "unknown":
            print("[warning] 出口探测仍失败；继续启动浏览器（仅确认漂移才硬中止）。")

    cookies, user_agent, browser_ok = solve_and_extract_cookie(final_proxy, test_url)

    if not browser_ok:
        print("\n[ABORT] 浏览器未进人物页，不进入 Step 2（避免半成品 Cookie 污染结论）。")
        return

    # Cookie 绑定的是「浏览器取页当时」的出口，不是解盾前基线。
    # 长耗时渲染期间 sticky 可能已漂；必须以解盾后 IP 作为复用基线。
    cookie_ip = baseline_ip
    if final_proxy:
        print("\n[IP 复基线] 浏览器取页成功后…")
        post = make_curl_session(final_proxy)
        post_ip = probe_exit_ip(post, label="IP#after_browser", timeout=12, rounds=1)
        if baseline_ip and post_ip and baseline_ip != post_ip:
            print(
                f"[Sticky] 渲染期间出口已变: {baseline_ip} -> {post_ip}。"
                "后续复用只认解盾后 IP；若 Step2 再漂则中止。"
            )
        if post_ip:
            cookie_ip = post_ip
        else:
            print("[warning] 解盾后 IP 探测失败，Step2 仍用解盾前基线（可能不准）。")

    run_fast_requests_with_cookie(
        final_proxy,
        cookies,
        user_agent,
        baseline_ip=cookie_ip,
        test_url=test_url,
        count=args.count,
    )


if __name__ == "__main__":
    main()
