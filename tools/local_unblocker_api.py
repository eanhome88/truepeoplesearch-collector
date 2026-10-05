#!/usr/bin/env python3
"""
私有本地双引擎抓取网关 (Local Unblocker API)
------------------------------------------------------------------
调度：
1. 引擎 A (curl_cffi)：同 sticky 出口上协议快路径；可用缓存 Cookie（不强制 cf_clearance）。
2. 引擎 B (StealthyFetcher)：协议被拦/失败时，无头 Chrome 同 sticky 内直接取 HTML 返回。
3. 降级不跨栈：浏览器成功后本请求直接交页；Cookie 仅缓存供下次协议尝试，不当作「解完再灌 curl」完成当次请求。
4. 粘性：穿云住宅用 `_s{sid}-{分钟}m`（见 tools/tps_poc_common.py），不用错误的 `-session-`。

启动：
    python tools/local_unblocker_api.py --port 8088

调用：
    curl "http://127.0.0.1:8088/v1/scrape?url=https://www.truepeoplesearch.com/find/person/..."
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from tps_env import load_project_env

    load_project_env(ROOT)
except Exception:
    pass

from flask import Flask, Response, jsonify, request

from tps_poc_common import (
    is_valid_person_response,
    make_curl_session,
    probe_exit_ip,
    sticky_proxy_url,
)

try:
    from curl_cffi import requests as c_requests
except ImportError:
    print("[ERROR] 缺少 curl_cffi 依赖，请运行: pip install curl-cffi")
    sys.exit(1)

try:
    from scrape_to_tidb import StealthyFetcher
except Exception as exc:
    StealthyFetcher = None
    print(f"[WARNING] StealthyFetcher 未初始化: {exc}")


app = Flask(__name__)

START_TIME = time.time()


def _build_info() -> dict:
    """启动时算一次：短 commit + 分支 + 脏标记 + 本文件 mtime。
    /health 直接展示，改完代码没重启一眼看穿。"""
    info = {"short_commit": "", "branch": "", "dirty": None,
            "file_mtime": 0, "version": ""}
    try:
        try:
            info["file_mtime"] = int(os.path.getmtime(__file__))
        except Exception:
            pass
        vjson = ROOT / "version.json"
        if vjson.exists():
            try:
                import json as _json

                info["version"] = str(_json.loads(vjson.read_text(encoding="utf-8")).get("version", ""))
            except Exception:
                pass
        if not (ROOT / ".git").exists():
            return info

        def _run(*args: str) -> str:
            try:
                out = subprocess.run(
                    list(args), cwd=str(ROOT), stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, timeout=2,
                )
                return out.stdout.strip() if out.returncode == 0 else ""
            except Exception:
                return ""

        info["short_commit"] = _run("git", "rev-parse", "--short", "HEAD")
        info["branch"] = _run("git", "rev-parse", "--abbrev-ref", "HEAD")
        info["dirty"] = bool(_run("git", "status", "--porcelain"))
    except Exception:
        pass
    return info


BUILD_INFO = _build_info()


def _parser_coverage() -> dict:
    """滚动解析覆盖率（协议端 record_coverage 写入），掉阈值直接在 health 里报警。"""
    try:
        r = get_redis_optional()
        if r is None:
            return {}
        total = int(r.get("tps:cov:total") or 0)
        if not total:
            return {"total": 0}
        out: dict = {"total": total, "fields": {}}
        for f in ("full_name", "primary_phone", "phone_numbers", "current_address", "emails"):
            try:
                n = int(r.get(f"tps:cov:{f}") or 0)
            except Exception:
                n = 0
            out["fields"][f] = {"filled": n, "rate": round(n / total, 4)}
        alerts = [f for f, lim in
                  (("full_name", 0.9), ("primary_phone", 0.4), ("phone_numbers", 0.4))
                  if out["fields"][f]["rate"] < lim]
        out["alerts"] = alerts
        return out
    except Exception:
        return {}
METRICS_LOCK = threading.Lock()
METRICS = {
    "total_requests": 0,
    "fast_protocol_hits": 0,
    "headless_browser_solves": 0,
    "blocked_requests": 0,
    "error_requests": 0,
}

SESSION_CACHE_LOCK = threading.Lock()
SESSION_CACHE: Dict[str, dict] = {}
CACHE_TTL_SEC = 1500
# InternalCaptcha / 死出口时最多换多少个新 sticky（可用环境变量覆盖）
MAX_IP_ROTATIONS = max(1, min(int(os.environ.get("UNBLOCKER_IP_RETRIES", "5") or "5"), 12))
# 预检开关：设 UNBLOCKER_PREFLIGHT=0 可跳过 ipify 预检（省住宅流量，默认开启）
PREFLIGHT_ENABLED = (os.environ.get("UNBLOCKER_PREFLIGHT", "1") or "1").strip() not in ("0", "false", "no")
# 浏览器并发上限：无头 Chrome 又重又慢，超过上限直接 503 快失败，别把本机打死
# 默认 4：协议快路径本就不占槽位，只有撞二次门才进浏览器；本机 8 核 16G 实测 4 并发稳定
MAX_BROWSERS = max(1, min(int(os.environ.get("UNBLOCKER_MAX_BROWSERS", "4") or "4"), 8))
BROWSER_SEMAPHORE = threading.Semaphore(MAX_BROWSERS)
BROWSER_TIMEOUT_MS = max(30000, min(int(os.environ.get("UNBLOCKER_BROWSER_TIMEOUT_MS", "75000") or "75000"), 120000))
# 单请求总预算：超过即停止换 IP，避免个别号码拖出 400 秒级长尾（默认 8 分钟）
REQUEST_BUDGET_MS = max(60000, min(int(os.environ.get("UNBLOCKER_BUDGET_MS", "480000") or "480000"), 1200000))
# 批量接口单次最多 URL 数，防止一次提交把浏览器池堵死
MAX_BATCH_URLS = max(1, min(int(os.environ.get("UNBLOCKER_MAX_BATCH", "20") or "20"), 50))
# Redis 可选：有则冷却/IP 统计多进程共享，没有则自动降级为内存
_REDIS_CLIENT = None
_REDIS_LOCK = threading.Lock()
REDIS_COOLDOWN_PREFIX = "unblocker:ip:cooldown:"
REDIS_IPSTATS_PREFIX = "unblocker:ip:stats:"

# IP 智能冷却与复用池 (Key: ip_address, Value: cooldown_until_timestamp)
IP_COOLDOWN_LOCK = threading.Lock()
IP_COOLDOWN_MAP: Dict[str, float] = {}
DEFAULT_IP_COOLDOWN_SEC = 1800.0  # 被拦 IP 默认冷却 30 分钟后自动解冻复用


def _sweep_expired_ip_cooldown_locked(now: float) -> None:
    expired = [k for k, v in IP_COOLDOWN_MAP.items() if now >= v]
    for k in expired:
        IP_COOLDOWN_MAP.pop(k, None)
    while len(IP_COOLDOWN_MAP) > 2000:
        IP_COOLDOWN_MAP.pop(next(iter(IP_COOLDOWN_MAP)))


def mark_ip_cooldown(ip: str, cooldown_sec: float = DEFAULT_IP_COOLDOWN_SEC) -> None:
    """将触发拦截/异常的 IP 打入冷却池，指定冷却时间 (默认 30 分钟)。"""
    if not ip:
        return
    now = time.time()
    with IP_COOLDOWN_LOCK:
        _sweep_expired_ip_cooldown_locked(now)
        IP_COOLDOWN_MAP[ip] = now + cooldown_sec
    try:
        r = get_redis_optional()
        if r is not None:
            r.set(REDIS_COOLDOWN_PREFIX + ip, "1", ex=int(cooldown_sec))
    except Exception:
        pass
    print(f"[IP-COOLDOWN] 出口 IP {ip} 已打入冷却池，将于 {cooldown_sec/60:.1f} 分钟后恢复可用")


def is_ip_in_cooldown(ip: str) -> tuple[bool, float]:
    """判断 IP 是否处于冷却中。若已过冷却期，自动解冻并重新放回可用池。"""
    if not ip:
        return False, 0.0
    try:
        r = get_redis_optional()
        if r is not None:
            ttl = r.ttl(REDIS_COOLDOWN_PREFIX + ip)
            if ttl is not None and int(ttl) > 0:
                return True, float(ttl)
    except Exception:
        pass
    with IP_COOLDOWN_LOCK:
        until = IP_COOLDOWN_MAP.get(ip)
        if not until:
            return False, 0.0
        now = time.time()
        if now >= until:
            IP_COOLDOWN_MAP.pop(ip, None)
            print(f"[IP-RECYCLE] 出口 IP {ip} 冷却期满，自动恢复可用！")
            return False, 0.0
        return True, round(until - now, 1)


WARMED_TTL_SEC = 1500


def publish_warmed_session(target_url: str, cookies: dict, user_agent: str, sid: str = "") -> None:
    """浏览器暖机成功后，把 Cookie 发布到 Redis，供协议 fleet 直接复用跑批量。"""
    if not cookies:
        return
    try:
        host = urlparse(target_url).netloc.lower() or "www.truepeoplesearch.com"
        import json

        payload = json.dumps({
            "cookies": dict(cookies),
            "user_agent": user_agent or "",
            "sid": sid or "",
            "ts": time.time(),
        }, ensure_ascii=False)
        r = get_redis_optional()
        if r is not None:
            r.set(f"unblocker:warmed:{host}", payload, ex=WARMED_TTL_SEC)
            if sid:
                r.set(f"unblocker:warmed:{host}:{sid[:12]}", payload, ex=WARMED_TTL_SEC)
            print(f"[WARMED] 已发布暖机 Cookie host={host} cookies={len(cookies)}")
    except Exception as exc:
        print(f"[WARMED] 发布失败: {type(exc).__name__}")


def record_ip_result(ip: str, ok: bool) -> None:
    """记录每个出口 IP 的成功/失败次数，用于 /health 观察和后续优选。"""
    if not ip:
        return
    try:
        r = get_redis_optional()
        if r is not None:
            key = REDIS_IPSTATS_PREFIX + ip
            r.hincrby(key, "ok" if ok else "fail", 1)
            r.expire(key, 86400)
    except Exception:
        pass
    with METRICS_LOCK:
        bucket = METRICS.setdefault("ip_stats_mem", {})
        entry = bucket.setdefault(ip, {"ok": 0, "fail": 0})
        entry["ok" if ok else "fail"] += 1
        # 内存版只留最近 2000 个 IP，长期运行不泄漏（Redis 版有 24h 过期兜底）
        while len(bucket) > 2000:
            bucket.pop(next(iter(bucket)))


def _default_proxy() -> str:
    return (os.environ.get("CLOUDBYPASS_PROXY") or os.environ.get("PROXY_TUNNEL") or "").strip()


def _proxy_pool() -> list:
    """多账号池：主账号 + CLOUDBYPASS_PROXY_LIST（逗号/换行分隔）。
    单账号今天照常跑，加账号明天自动分流（按 session 亲和，同一 lane 固定同一账号）。"""
    pool: list = []
    primary = _default_proxy()
    if primary:
        pool.append(primary)
    extra = (os.environ.get("CLOUDBYPASS_PROXY_LIST") or "").replace("\n", ",")
    for chunk in extra.split(","):
        cand = chunk.strip()
        if cand and cand not in pool:
            pool.append(cand)
    return pool


def _pool_pick(session_id: Optional[str]) -> Optional[str]:
    """按 session 稳定选账号：lane 亲和不断，账号越多分流越散。"""
    pool = _proxy_pool()
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    digest = hashlib.md5((session_id or "local1").encode("utf-8")).digest()
    return pool[digest[0] % len(pool)]


def _pool_info() -> dict:
    """脱敏：只暴露账号个数和 host，不过密码。"""
    hosts = []
    for p in _proxy_pool():
        try:
            hosts.append(urlparse(p).hostname or "?")
        except Exception:
            hosts.append("?")
    return {"accounts": len(hosts), "hosts": sorted(set(hosts))}



def bind_sticky_proxy(proxy_url: Optional[str], session_id: Optional[str], minutes: int = 30) -> Optional[str]:
    """按网关类型写入粘性；穿云住宅为 _s…-Nm。"""
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    sid = (session_id or "local1").strip() or "local1"
    return sticky_proxy_url(raw, sid, minutes=minutes)


def get_cache_key(url: str, proxy_url: Optional[str]) -> str:
    # Cookie 与出口 IP 绑定：缓存键必须带完整 sticky（同 sid=同出口才复用）。
    # 复用方式是调用方传相同 session_id（lane 亲和），而不是跨 sticky 混用。
    host = urlparse(url).netloc.lower()
    return f"{host}::{proxy_url or 'direct'}"


def _subnet_of(ip: str) -> str:
    parts = (ip or "").strip().split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3])
    return (ip or "").strip()


def get_redis_optional():
    """Redis 可用就返回 client，否则返回 None（调用方必须能降级）。"""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        try:
            _REDIS_CLIENT.ping()
            return _REDIS_CLIENT
        except Exception:
            _REDIS_CLIENT = None
    try:
        import redis as redis_lib  # type: ignore
    except ImportError:
        return None
    with _REDIS_LOCK:
        if _REDIS_CLIENT is not None:
            return _REDIS_CLIENT
        try:
            client = redis_lib.Redis(
                host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                port=int(os.environ.get("REDIS_PORT", "6379") or "6379"),
                password=os.environ.get("REDIS_PASSWORD") or None,
                decode_responses=True,
                socket_connect_timeout=0.4,
                socket_timeout=0.8,
            )
            client.ping()
            _REDIS_CLIENT = client
            return client
        except Exception:
            return None


REDIS_SESS_PREFIX = "unblocker:sess:"
REDIS_METRICS_KEY = "unblocker:metrics"


def _sess_redis_key(cache_key: str) -> str:
    return REDIS_SESS_PREFIX + cache_key


def get_cached_session(cache_key: str) -> Optional[dict]:
    # 多 worker/多副本共享：优先 Redis，内存只做本地兜底
    try:
        r = get_redis_optional()
        if r is not None:
            import json as _json

            raw = r.get(_sess_redis_key(cache_key))
            if raw:
                entry = _json.loads(raw)
                if time.time() - float(entry.get("timestamp", 0)) <= CACHE_TTL_SEC:
                    return entry
    except Exception:
        pass
    with SESSION_CACHE_LOCK:
        entry = SESSION_CACHE.get(cache_key)
        if not entry:
            return None
        if time.time() - entry.get("timestamp", 0) > CACHE_TTL_SEC:
            SESSION_CACHE.pop(cache_key, None)
            return None
        return entry


def set_cached_session(cache_key: str, cookies: dict, user_agent: str, sid: str = "") -> None:
    """缓存整套 Cookie，不要求必须有 cf_clearance（干净 IP 可能无挑战）。"""
    if not cookies:
        return
    entry = {
        "cookies": dict(cookies),
        "user_agent": user_agent or "",
        "timestamp": time.time(),
        # cf_clearance 与 IP 绑定：复用时必须回到同一个 sid（同一出口）才有效
        "sid": sid or "",
    }
    with SESSION_CACHE_LOCK:
        SESSION_CACHE[cache_key] = entry
    try:
        r = get_redis_optional()
        if r is not None:
            import json as _json

            r.set(_sess_redis_key(cache_key), _json.dumps(entry), ex=CACHE_TTL_SEC)
    except Exception:
        pass


def drop_cached_session(cache_key: str) -> None:
    with SESSION_CACHE_LOCK:
        SESSION_CACHE.pop(cache_key, None)
    try:
        r = get_redis_optional()
        if r is not None:
            r.delete(_sess_redis_key(cache_key))
    except Exception:
        pass


def incr_shared_metric(name: str, amount: int = 1) -> None:
    """多 worker 全局计数（Redis），/health 合并展示，无 Redis 则只留内存。"""
    with METRICS_LOCK:
        METRICS[name] = int(METRICS.get(name, 0)) + amount
    try:
        r = get_redis_optional()
        if r is not None:
            r.hincrby(REDIS_METRICS_KEY, name, amount)
    except Exception:
        pass


def read_shared_metrics() -> dict:
    try:
        r = get_redis_optional()
        if r is not None:
            return {k: int(v) for k, v in (r.hgetall(REDIS_METRICS_KEY) or {}).items()}
    except Exception:
        pass
    return {}


def is_valid_response(status_code: int, text: str, url: str = "") -> tuple[bool, str, int]:
    """
    返回 (ok_for_delivery, reason, http_code_to_client)。
    404/410 视为目标站正常终态（可交付），验证页不可交付。
    """
    if status_code in (404, 410):
        return True, f"目标站终态 HTTP {status_code}", status_code
    if "/person/" in (url or ""):
        ok, reason = is_valid_person_response(status_code, text, url)
        return ok, reason, (200 if ok else status_code or 403)
    if status_code != 200:
        return False, f"HTTP状态码异常 ({status_code})", status_code or 403
    lower = (text or "").lower()
    for marker in (
        "internalcaptcha",
        "just a moment",
        "cf-turnstile",
        "cf-challenge",
        "attention required",
        "checking your browser",
    ):
        if marker in lower:
            return False, f"命中验证/拦截标记 '{marker}'", 403
    if len(text or "") < 500:
        return False, "页面过短", 403
    return True, "抓取成功", 200


def _page_html(page) -> str:
    for attr in ("html_content", "html", "body", "content", "text"):
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


def try_protocol_fetch(
    target_url: str,
    proxy_url: Optional[str],
    cookies: Optional[dict] = None,
    user_agent: str = "",
    referer: Optional[str] = None,
) -> Tuple[bool, str, int, str]:
    """引擎 A：协议层。返回 (ok, html_or_reason, status, detail)。"""
    ua = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
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
        "referer": referer or "https://www.google.com/",
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    session = c_requests.Session(impersonate="chrome124", proxies=proxies)
    if cookies:
        session.cookies.update(cookies)
    try:
        resp = session.get(target_url, headers=headers, timeout=20)
        ok, reason, code = is_valid_response(resp.status_code, resp.text, target_url)
        if ok:
            return True, resp.text, code, reason
        return False, resp.text or reason, code, reason
    except Exception as exc:
        return False, "", 502, f"protocol error: {exc}"


def _page_final_url(page, fallback: str = "") -> str:
    for attr in ("url", "final_url"):
        try:
            val = getattr(page, attr, None)
        except Exception:
            continue
        if callable(val):
            try:
                val = val()
            except Exception:
                continue
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        resp = getattr(page, "response", None)
        if resp is not None:
            u = getattr(resp, "url", None)
            if isinstance(u, str) and u.strip():
                return u.strip()
    except Exception:
        pass
    return fallback or ""


def fetch_with_browser(target_url: str, proxy_url: Optional[str]) -> Tuple[bool, str, dict, str, int]:
    """
    引擎 B：无头 Chrome 同 sticky 内直接取页。
    成功时返回的 HTML 即本请求交付内容（不灌回 curl 完成当次请求）。
    """
    if StealthyFetcher is None:
        return False, "StealthyFetcher 模块未就绪", {}, "", 503

    # 并发上限：拿不到槽位直接快失败，避免把本机 Chrome 全部拖死
    if not BROWSER_SEMAPHORE.acquire(blocking=True, timeout=30):
        return False, "browser pool busy, try again later", {}, "", 503

    try:
        return _fetch_with_browser_locked(target_url, proxy_url)
    finally:
        BROWSER_SEMAPHORE.release()


def _fetch_with_browser_locked(target_url: str, proxy_url: Optional[str]) -> Tuple[bool, str, dict, str, int]:
    print(f"[ENGINE-B] 无头 Chrome 同 sticky 取页: {target_url[:80]}...")

    def _wait_person_markers(pw_page, timeout_s: float = 12.0) -> bool:
        """人物页内容是异步渲染的：跳转后等 Phone Numbers/Current Address 出现再交页。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                content = (pw_page.content() or "").lower()
            except Exception:
                return False
            if "phone numbers" in content and "current address" in content:
                return True
            try:
                pw_page.wait_for_timeout(800)
            except Exception:
                return False
        return False

    def _try_click_turnstile_checkbox(pw_page) -> bool:
        """Turnstile 转人工勾选时点一次框（站内自带 widget 的正常交互，不是盲提交）。"""
        try:
            for frame in pw_page.frames:
                if "challenges.cloudflare.com" not in (frame.url or ""):
                    continue
                for sel in ("input[type='checkbox']", "label", "span.cb-lb", "div.cb-i"):
                    try:
                        loc = frame.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=3000)
                            print(f"[ENGINE-B] 已点 Turnstile 勾选框 ({sel})")
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        return False

    def auto_submit_captcha(playwright_page):
        """
        InternalCaptcha = 站内 Turnstile widget + XHR 带 captchaToken 提交，服务端验票后才放 redirectUrl。
        无 token 的盲点 Submit 必失败；正确做法是给页面自带流程留出时间：
        widget 自动解 → 回调拿 token → XHR 提交 → 跳人物页（手动兜底 22s 才出现）。
        """
        try:
            for _ in range(20):
                url = (playwright_page.url or "").lower()
                if "internalcaptcha" in url or "/find/person/" in url:
                    break
                playwright_page.wait_for_timeout(400)

            url = (playwright_page.url or "").lower()
            if "internalcaptcha" not in url:
                # 直达人物页：等异步内容渲染完再交，否则 17KB 半成品会被判失败
                if "/find/person/" in url:
                    ok_mark = _wait_person_markers(playwright_page, 12.0)
                    print(f"[ENGINE-B] 直达人物页，内容渲染{'完成' if ok_mark else '未完成(12s 超时)'}")
                return

            print(f"[ENGINE-B] 命中 InternalCaptcha（站内 Turnstile）: {playwright_page.url[:140]}")
            t_start = time.time()
            deadline = t_start + 28.0
            clicked = False
            while time.time() < deadline:
                cur = (playwright_page.url or "").lower()
                if "internalcaptcha" not in cur and "/find/person/" in cur:
                    print(f"[ENGINE-B] Turnstile 解开并跳转 → {playwright_page.url[:120]}")
                    ok_mark = _wait_person_markers(playwright_page, 12.0)
                    print(f"[ENGINE-B] 跳转后内容渲染{'完成' if ok_mark else '未完成(12s 超时)'}")
                    return
                # 8s 后若 widget 转人工交互，点一次勾选框
                if not clicked and time.time() - t_start > 8.0:
                    clicked = _try_click_turnstile_checkbox(playwright_page)
                playwright_page.wait_for_timeout(1000)
            print(f"[ENGINE-B] 28s 内 Turnstile 未解开，仍停在: {(playwright_page.url or '')[:120]} → 换出口重试")
        except Exception as exc:
            print(f"[ENGINE-B] InternalCaptcha 处理异常: {exc}")

    try:
        page = StealthyFetcher.fetch(
            target_url,
            proxy=proxy_url,
            solve_cloudflare=True,
            page_action=auto_submit_captcha,
            headless=True,
            # network_idle 在广告多的人物页上容易挂死；用 load + 较短超时
            network_idle=False,
            timeout=BROWSER_TIMEOUT_MS,
        )
    except Exception as exc:
        print(f"[ENGINE-B] 异常: {exc}")
        return False, f"Headless browser error: {exc}", {}, "", 502

    body = _page_html(page)
    status = getattr(page, "status", None)
    if status is None:
        status = getattr(getattr(page, "response", None), "status", 0)
    try:
        status = int(status)
    except Exception:
        status = 0
    cookies = _extract_cookies(page)
    ua = getattr(page, "user_agent", "") or ""
    final_url = _page_final_url(page, target_url)
    ok, reason, code = is_valid_response(status, body, final_url or target_url)
    if ok:
        print(
            f"[ENGINE-B] 直接交页成功 HTTP {status} | url={final_url[:80]} | cookies={len(cookies)} "
            f"| cf_clearance={'yes' if 'cf_clearance' in cookies else 'no'} | {reason}"
        )
        return True, body, cookies, ua, code
    print(f"[ENGINE-B] 未通过: {reason} | final_url={final_url[:120]}")
    return False, body or reason, cookies, ua, code


def _fresh_sticky_sid(prefix: str = "ip") -> str:
    """生成 8 位唯一 sticky id（穿云 _s 只用前 8 位，必须每次都新）。"""
    base = "".join(ch for ch in (prefix or "ip") if ch.isalnum())[:2] or "ip"
    return f"{base}{secrets.token_hex(3)}"[:8]


def is_valid_target_url(url: str) -> bool:
    """垃圾输入直接 400，别浪费一次住宅流量和浏览器启动。"""
    try:
        parts = urlparse((url or "").strip())
        return parts.scheme in ("http", "https") and bool(parts.netloc)
    except Exception:
        return False


def _should_rotate_ip(html_or_err: str, code: int) -> bool:
    """二次门 / 代理挂了 → 换新 IP；404/410 等终态不换。"""
    if code in (404, 410):
        return False
    blob = (html_or_err or "").lower()
    markers = (
        "internalcaptcha",
        "just a moment",
        "cf-turnstile",
        "attention required",
        "err_empty_response",
        "err_timed_out",
        "err_tunnel",
        "err_connection",
        "timed out",
        "timeout",
        "proxy/browser navigation timeout",
        "headless browser error",
    )
    return any(m in blob for m in markers) or code in (429, 502, 503)


def execute_unblocker_scrape(
    target_url: str,
    proxy_url: Optional[str] = None,
    session_id: Optional[str] = "local1",
    referer: Optional[str] = None,
    force_browser: bool = False,
) -> dict:
    """协议优先 → 失败则浏览器直接交页；二次门/死出口自动拉新 sticky IP。"""
    incr_shared_metric("total_requests")
    t0 = time.time()

    raw_proxy = (proxy_url or _pool_pick(session_id) or "").strip() or None
    base_sid = (session_id or "local1").strip() or "local1"
    attempt_sid = base_sid
    attempt_proxy = bind_sticky_proxy(raw_proxy, attempt_sid) if raw_proxy else None
    cache_key = get_cache_key(target_url, attempt_proxy)
    cached = None if force_browser else get_cached_session(cache_key)
    seen_ips: list[str] = []
    last: dict = {}

    def _mint_live_sticky(why: str) -> tuple[str, Optional[str], str]:
        """拉新 sticky；默认预检出口，跳过冷却/已用 IP/同 /24，支持解冻复用。"""
        seen_subnets = {_subnet_of(x) for x in seen_ips if x}
        for i in range(6):
            sid = _fresh_sticky_sid(base_sid)
            proxy = bind_sticky_proxy(raw_proxy, sid) if raw_proxy else None
            if not proxy:
                return sid, None, ""
            if PREFLIGHT_ENABLED:
                sess = make_curl_session(proxy)
                ip = probe_exit_ip(sess, label=f"new-ip-{sid}", timeout=8, rounds=1)
                if not ip:
                    print(f"[ROTATE] #{i+1} sticky={sid} 预检失败（{why}），再拉")
                    continue
            else:
                # 关预检时直接用新 sticky，把流量省给真实业务请求
                print(f"[ROTATE] #{i+1} sticky={sid} 跳过预检（UNBLOCKER_PREFLIGHT=0），直接用")
                return sid, proxy, ""

            in_cooling, remaining = is_ip_in_cooldown(ip)
            if in_cooling:
                print(f"[ROTATE] #{i+1} sticky={sid} IP {ip} 冷却中 (还需 {remaining}s)，跳过")
                continue
            if ip in seen_ips:
                print(f"[ROTATE] #{i+1} sticky={sid} IP {ip} 本次已用过，再拉")
                continue
            if _subnet_of(ip) in seen_subnets:
                print(f"[ROTATE] #{i+1} sticky={sid} IP {ip} 与已失败同 /24，再拉分散风险")
                continue
            print(f"[ROTATE] 新出口 sticky={sid} ip={ip} 原因={why}")
            return sid, proxy, ip
        return _fresh_sticky_sid(base_sid), None, ""


    # --- 首 sticky 先验活：死出口直接换，省一次浏览器空转（约 75 秒） ---
    if raw_proxy and attempt_proxy:
        try:
            first_ip = probe_exit_ip(
                make_curl_session(attempt_proxy),
                label=f"first-{attempt_sid[:12]}", timeout=8, rounds=1,
            )
        except Exception:
            first_ip = ""
        if first_ip:
            seen_ips.append(first_ip)
        else:
            print("[ROTATE] 首 sticky 出口不通，直接拉新 IP")
            attempt_sid, attempt_proxy, ip = _mint_live_sticky("initial-exit-dead")
            if attempt_proxy is None:
                attempt_sid = base_sid
                attempt_proxy = bind_sticky_proxy(raw_proxy, attempt_sid)
            elif ip:
                seen_ips.append(ip)
            cache_key = get_cache_key(target_url, attempt_proxy)
            cached = None if force_browser else get_cached_session(cache_key)

    # --- 引擎 A：仅第一次协议快路径（成功则直接返回）---
    if not force_browser:
        cookies = (cached or {}).get("cookies") if cached else None
        ua = (cached or {}).get("user_agent", "") if cached else ""
        label = "cached" if cookies else "cold"
        print(f"[ENGINE-A] 协议尝试 ({label}) sticky={'yes' if attempt_proxy else 'no'}")
        ok, html, code, detail = try_protocol_fetch(
            target_url, attempt_proxy, cookies=cookies, user_agent=ua, referer=referer
        )
        if ok:
            incr_shared_metric("fast_protocol_hits")
            return {
                "code": code,
                "engine": f"fast_protocol_{label}",
                "html": html,
                "bytes": len(html or ""),
                "elapsed_ms": round((time.time() - t0) * 1000.0, 1),
                "detail": detail,
                "proxy_bound": bool(attempt_proxy),
                "session_id": attempt_sid,
            }
        print(f"[ENGINE-A] 未通过 ({detail}) → 引擎 B；若二次门/死出口将自动换 IP（最多 {MAX_IP_ROTATIONS} 次）")
        if cached:
            drop_cached_session(cache_key)
        # 协议层已撞二次门：立刻换新 IP 再进浏览器，别在脏 sticky 上硬扛
        if raw_proxy and _should_rotate_ip(detail + (html or ""), code):
            if seen_ips:
                mark_ip_cooldown(seen_ips[-1], 1800.0)
            attempt_sid, attempt_proxy, ip = _mint_live_sticky(detail)
            if ip:
                seen_ips.append(ip)
            elif attempt_proxy is None:
                print("[ROTATE] 首轮换 IP 预检失败，仍用原 sticky 进浏览器碰一次")
                attempt_proxy = bind_sticky_proxy(raw_proxy, base_sid)
                attempt_sid = base_sid
            cache_key = get_cache_key(target_url, attempt_proxy)

    # --- 引擎 B：浏览器交页；失败则拉新 IP 重试（总预算熔断防长尾） ---
    for attempt in range(MAX_IP_ROTATIONS):
        if (time.time() - t0) * 1000.0 > REQUEST_BUDGET_MS:
            print(f"[BUDGET] 单请求超 {REQUEST_BUDGET_MS}ms 熔断，停止换 IP")
            break
        if attempt > 0:
            if not raw_proxy:
                print("[ROTATE] 无代理可换，停止")
                break
            why = (last.get("html_or_err") or "retry")[:80]
            if seen_ips:
                mark_ip_cooldown(seen_ips[-1], 1800.0)
            attempt_sid, attempt_proxy, ip = _mint_live_sticky(why)
            if not attempt_proxy:
                print("[ROTATE] 连续预检失败，停止换 IP")
                break
            if ip:
                seen_ips.append(ip)
            cache_key = get_cache_key(target_url, attempt_proxy)
            print(f"[ENGINE-B] 第 {attempt+1}/{MAX_IP_ROTATIONS} 次，新 sticky={attempt_sid}")


        success, html_or_err, cookies, user_agent, code = fetch_with_browser(
            target_url, attempt_proxy
        )
        elapsed_ms = round((time.time() - t0) * 1000.0, 1)
        last = {
            "success": success,
            "html_or_err": html_or_err,
            "cookies": cookies,
            "user_agent": user_agent,
            "code": code,
            "elapsed_ms": elapsed_ms,
            "proxy": attempt_proxy,
            "session_id": attempt_sid,
            "ips_tried": list(seen_ips),
            "attempts": attempt + 1,
        }
        if success:
            break
        if not _should_rotate_ip(html_or_err, code):
            print(f"[ROTATE] 非换 IP 类失败 ({code})，停止: {(html_or_err or '')[:100]}")
            break
        print(f"[ROTATE] 需要新 IP ({code})，已试 {attempt+1}/{MAX_IP_ROTATIONS}")

    if last.get("success"):
        incr_shared_metric("headless_browser_solves")
        # 同 sticky（同出口）下次协议快路径可命中；调用方传相同 session_id 即复用
        set_cached_session(cache_key, last["cookies"], last["user_agent"], sid=last.get("session_id", ""))
        publish_warmed_session(target_url, last["cookies"], last["user_agent"], last.get("session_id", ""))
        for ip in last.get("ips_tried") or []:
            record_ip_result(ip, True)
        return {
            "code": last["code"],
            "engine": "headless_browser_direct",
            "html": last["html_or_err"],
            "bytes": len(last["html_or_err"] or ""),
            "elapsed_ms": last["elapsed_ms"],
            "detail": "browser delivered HTML; cookies cached for next protocol try",
            "has_cf_clearance": "cf_clearance" in (last["cookies"] or {}),
            "cookie_count": len(last["cookies"] or {}),
            "proxy_bound": bool(last["proxy"]),
            "session_id": last["session_id"],
            "ips_tried": last.get("ips_tried") or [],
            "attempts": last.get("attempts", 1),
        }

    incr_shared_metric("blocked_requests")
    for ip in last.get("ips_tried") or []:
        record_ip_result(ip, False)
    err_body = last.get("html_or_err") or ""
    low = err_body.lower()
    if "internalcaptcha" in low:
        short_err = (
            f"TPS InternalCaptcha after {last.get('attempts', 1)} IP(s); "
            f"tried={last.get('ips_tried') or []}"
        )
    elif "timed_out" in low or "err_empty" in low or "timeout" in low:
        short_err = f"proxy timeout after {last.get('attempts', 1)} IP(s)"
    elif len(err_body) < 500:
        short_err = err_body
    else:
        short_err = "browser failed validation"
    return {
        "code": last["code"] if last.get("code") not in (0, 200) else 429,
        "engine": "headless_browser_failed",
        "error": short_err,
        "html": err_body if len(err_body) > 100 else "",
        "bytes": len(err_body),
        "elapsed_ms": last.get("elapsed_ms", round((time.time() - t0) * 1000.0, 1)),
        "proxy_bound": bool(last.get("proxy")),
        "session_id": last.get("session_id"),
        "ips_tried": last.get("ips_tried") or [],
        "attempts": last.get("attempts", 0),
    }


@app.route("/v1/scrape", methods=["GET", "POST"])
def api_scrape():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        target_url = payload.get("url") or payload.get("target_url")
        proxy_url = payload.get("proxy") or payload.get("proxy_url")
        session_id = payload.get("session_id", "local1")
        referer = payload.get("referer")
        force_browser = bool(payload.get("force_browser", False))
        want_json = bool(payload.get("json", False))
    else:
        target_url = request.args.get("url") or request.args.get("target_url")
        proxy_url = request.args.get("proxy") or request.args.get("proxy_url")
        session_id = request.args.get("session_id", "local1")
        referer = request.args.get("referer")
        force_browser = request.args.get("force_browser", "0") in ("1", "true", "yes")
        want_json = request.args.get("json", "0") in ("1", "true", "yes")

    if not target_url:
        return jsonify({"code": 400, "error": "Missing required parameter: url"}), 400
    if not is_valid_target_url(target_url):
        return jsonify({"code": 400, "error": "Invalid url (need http(s)://host...)"}), 400

    result = execute_unblocker_scrape(
        target_url=target_url,
        proxy_url=proxy_url,
        session_id=session_id,
        referer=referer,
        force_browser=force_browser,
    )

    http_status = 200 if result.get("code") in (200, 404, 410) else int(result.get("code") or 429)

    if want_json:
        return jsonify(result), http_status

    if result.get("code") in (200, 404, 410) and result.get("html"):
        return Response(result["html"], status=int(result["code"]), mimetype="text/html; charset=utf-8")
    # 失败页可能很大（整张验证页 HTML），非 JSON 模式只给前 8KB 定位用
    fail_html = result.get("html") or ""
    if len(fail_html) > 8192:
        fail_html = fail_html[:8192] + "\n<!-- ...truncated... -->"
    return Response(
        fail_html or f"<!-- Failed: {result.get('error')} -->",
        status=http_status,
        mimetype="text/html; charset=utf-8",
    )


@app.route("/v1/scrape_batch", methods=["POST"])
def api_scrape_batch():
    """批量抓取：协议快路径天然高并发，只有撞二次门的才排队进浏览器池。"""
    from concurrent.futures import ThreadPoolExecutor

    payload = request.get_json(silent=True) or {}
    urls = payload.get("urls") or payload.get("targets") or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    if not urls:
        return jsonify({"code": 400, "error": "Missing required parameter: urls[]"}), 400
    bad = [u for u in urls if not is_valid_target_url(u)]
    if bad:
        return jsonify({"code": 400, "error": f"Invalid url(s): {bad[0][:80]}"}), 400
    if len(urls) > MAX_BATCH_URLS:
        return jsonify({"code": 400, "error": f"too many urls (max {MAX_BATCH_URLS})"}), 400

    proxy_url = payload.get("proxy") or payload.get("proxy_url")
    prefix = (payload.get("session_prefix") or payload.get("session_id") or "batch").strip() or "batch"
    referer = payload.get("referer")
    force_browser = bool(payload.get("force_browser", False))
    # 按 lane 散列 session：同 lane 复用同一 sticky+缓存，不同 lane 用不同出口
    lanes = max(1, min(int(payload.get("lanes", MAX_BROWSERS) or MAX_BROWSERS), 8))
    want_html = bool(payload.get("html", False))
    t0 = time.time()

    def _one(idx_url):
        idx, url = idx_url
        sid = f"{prefix}-lane{idx % lanes}"
        try:
            r = execute_unblocker_scrape(
                target_url=url, proxy_url=proxy_url, session_id=sid,
                referer=referer, force_browser=force_browser,
            )
        except Exception as exc:
            r = {"code": 500, "engine": "batch_error", "error": f"{type(exc).__name__}: {exc}", "url": url}
        r = dict(r)
        r["url"] = url
        if not want_html:
            r.pop("html", None)
        return r

    with ThreadPoolExecutor(max_workers=min(len(urls), MAX_BROWSERS * 2)) as pool:
        items = list(pool.map(_one, list(enumerate(urls))))

    ok = sum(1 for r in items if r.get("code") in (200, 404, 410))
    incr_shared_metric("batch_requests")
    return jsonify({
        "code": 200,
        "ok_count": ok,
        "total": len(items),
        "elapsed_ms": round((time.time() - t0) * 1000.0, 1),
        "lanes": lanes,
        "items": items,
    })


@app.route("/v1/warmed", methods=["GET"])
def api_warmed():
    """协议 fleet 查询暖机 Cookie：GET /v1/warmed?host=...&sid=...（sid 优先命中同出口）"""
    host = (request.args.get("host") or "www.truepeoplesearch.com").lower()
    sid = (request.args.get("sid") or "").strip()[:12]
    try:
        r = get_redis_optional()
        if r is not None:
            keys = ([f"unblocker:warmed:{host}:{sid}"] if sid else []) + [f"unblocker:warmed:{host}"]
            for key in keys:
                raw = r.get(key)
                if raw:
                    import json

                    data = json.loads(raw)
                    data["host"] = host
                    data["cookie_count"] = len(data.get("cookies") or {})
                    return jsonify({"code": 200, **data})
    except Exception:
        pass
    with SESSION_CACHE_LOCK:
        for key, entry in SESSION_CACHE.items():
            if key.startswith(host + "::"):
                # 内存兜底同样查 TTL：过期 Cookie 不得复用（与 Redis 路径一致）
                try:
                    age = time.time() - float(entry.get("timestamp", 0))
                except Exception:
                    continue
                if age > CACHE_TTL_SEC:
                    continue
                return jsonify({
                    "code": 200, "host": host, "cookies": entry.get("cookies") or {},
                    "user_agent": entry.get("user_agent") or "", "source": "memory",
                    "cookie_count": len(entry.get("cookies") or {}),
                })
    return jsonify({"code": 404, "error": "no warmed session, hit /v1/scrape once to warm"}), 404


@app.route("/health", methods=["GET"])
def api_health():
    uptime = time.time() - START_TIME
    with METRICS_LOCK:
        snapshot = METRICS.copy()
    # 多 worker 全局计数优先 Redis（无 Redis 时本 worker 内存值即全部）
    shared = read_shared_metrics()
    total = int(shared.get("total_requests", snapshot.get("total_requests", 0)))
    succ = int(shared.get("fast_protocol_hits", snapshot.get("fast_protocol_hits", 0))) + \
        int(shared.get("headless_browser_solves", snapshot.get("headless_browser_solves", 0)))
    succ_rate = (succ / total * 100.0) if total > 0 else 100.0
    with SESSION_CACHE_LOCK:
        cache_count = len(SESSION_CACHE)
    with IP_COOLDOWN_LOCK:
        cooldown_count = len(IP_COOLDOWN_MAP)
    redis_on = get_redis_optional() is not None
    return jsonify({
        "status": "ok",
        "service": "local dual-engine unblocker",
        "uptime_seconds": round(uptime, 1),
        "active_session_cache": cache_count,
        "metrics": snapshot,
        "metrics_global": shared or snapshot,
        "success_rate_percent": round(succ_rate, 2),
        "sticky": "cloudbypass _s / region -sid / generic -session_",
        "policy": "protocol first; browser delivers HTML directly on fallback",
        "ip_cooldown_count_mem": cooldown_count,
        "redis_shared": redis_on,
        "browser_pool": {"max": MAX_BROWSERS, "timeout_ms": BROWSER_TIMEOUT_MS,
                           "request_budget_ms": REQUEST_BUDGET_MS},
        "preflight": PREFLIGHT_ENABLED,
        "max_rotations": MAX_IP_ROTATIONS,
        "max_batch_urls": MAX_BATCH_URLS,
        "proxy_pool": _pool_info(),
        "build": BUILD_INFO,
        "parser_coverage": _parser_coverage(),
    })


def main():
    parser = argparse.ArgumentParser(description="本地双引擎抓取 API 网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()

    print("=" * 70)
    print("本地双引擎网关已就绪")
    print(f"  监听: http://{args.host}:{args.port}")
    print(f"  抓取: http://{args.host}:{args.port}/v1/scrape?url=<URL>&session_id=s1")
    print(f"  健康: http://{args.host}:{args.port}/health")
    print("  策略: 协议优先 → 失败则浏览器交页；InternalCaptcha/死出口自动换 sticky IP")
    print(f"  换 IP: 最多 {MAX_IP_ROTATIONS} 次（UNBLOCKER_IP_RETRIES），预检={'开' if PREFLIGHT_ENABLED else '关(UNBLOCKER_PREFLIGHT=0)'}")
    print(f"  浏览器并发上限: {MAX_BROWSERS}（UNBLOCKER_MAX_BROWSERS）")
    print("  粘性: 穿云住宅 _s{sid}-{m}m（tps_poc_common）")
    print("  量产建议: gunicorn -w 2 --threads 4 -b 127.0.0.1:8088 tools.local_unblocker_api:app")
    print("=" * 70)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
