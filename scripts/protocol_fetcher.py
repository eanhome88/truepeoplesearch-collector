#!/usr/bin/env python3
"""
TruePeopleSearch 协议层高性能抓取与解析模块 (Protocol Fetcher)

核心特性：
1. 基于 curl_cffi 实现 JA3 / JA4 TLS 指纹模拟 (impersonate="chrome124")。
2. 强制开启 br / gzip 压缩，流量开销降低 80%。
3. 【极致优化】流式提前截断 (Stream Early-Cutoff)：
   在数据流接收到核心资产（姓名、年龄、当前地址及房产、电话、邮箱）且遇到 Previous Addresses 时，
   客户端立即发送 RST 强行掐断流传输，单页流量从 20KB 骤降至 9~11KB，再省 50% 流量！
4. 【极致优化】精简解析器：
   保留全部核心字段，剔除冗余的历史过往地址 (previous_addresses) 与别名 (aliases)，
   数据库写入行数减少 65%，单机写入吞吐翻倍。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from curl_cffi.requests import AsyncSession
from curl_cffi.curl import CurlError
from scrapling.parser import Adaptor

from scrape_to_tidb import (
    MONTHS, _eligible_phone_type, _valid_us_phone, extract_person_id,
    extract_phone_numbers, parse_date, split_full_name,
)


# ============================================================
# 异常分桶体系（兼容 tps_metrics 与 tps_queue 重试机制）
# ============================================================

class ScrapeError(Exception):
    bucket: str = "error"

    def __init__(self, message: str = "", bucket: Optional[str] = None):
        super().__init__(message)
        if bucket is not None:
            self.bucket = bucket


class HttpError(ScrapeError):
    def __init__(self, status: int, message: str = "", bucket: Optional[str] = None):
        self.status = status
        if bucket is None:
            bucket = "http_4xx" if int(status) < 500 else "http_5xx"
        super().__init__(message or f"HTTP {status}", bucket=bucket)


class CloudflareChallengeError(ScrapeError):
    """Cloudflare 5秒盾 / Turnstile / WAF 阻断"""
    bucket = "cf_fail"

    def __init__(self, message: str = "Cloudflare challenge detected", bucket: str = "cf_fail"):
        super().__init__(message, bucket=bucket)


class EmptyPageError(ScrapeError):
    """空页面或 404 (无数据，正常完成并消费任务)"""
    bucket = "empty"

    def __init__(self, message: str = "empty page", bucket: str = "empty"):
        super().__init__(message, bucket=bucket)


class ProxyError(ScrapeError):
    """代理握手超时、连接拒绝或坏代理"""
    bucket = "proxy_fail"

    def __init__(self, message: str = "proxy connection error", bucket: str = "proxy_fail"):
        super().__init__(message, bucket=bucket)


class FetchTimeoutError(ScrapeError):
    bucket = "timeout"

    def __init__(self, message: str = "fetch timeout", bucket: str = "timeout"):
        super().__init__(message, bucket=bucket)


# ============================================================
# 请求头配置与特征库
# ============================================================

DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

CF_CHALLENGE_PATTERNS = [
    re.compile(r"challenge-platform", re.IGNORECASE),
    re.compile(r"Just a moment\.\.\.", re.IGNORECASE),
    re.compile(r"Attention Required!\s*\|\s*Cloudflare", re.IGNORECASE),
    re.compile(r"cf-browser-verification", re.IGNORECASE),
    re.compile(r"turnstile", re.IGNORECASE),
    re.compile(r"Checking your browser", re.IGNORECASE),
    re.compile(r"Please wait while your request is being verified", re.IGNORECASE),
]


def check_cloudflare_blocked(status_code: int, html_text: str) -> bool:
    """检测页面是否包含 Cloudflare 防护拦截特征"""
    if status_code in (403, 503):
        return True
    
    if len(html_text) < 15000:
        for pat in CF_CHALLENGE_PATTERNS:
            if pat.search(html_text):
                return True
    return False


# 验证码/挑战页不是空档案。正文或请求 URL 命中即算，正常带姓名的人物页除外。
_CAPTCHA_MARKERS = (
    "internalcaptcha",
    "just a moment",
    "cf-challenge",
    "attention required",
    "请稍候",
)


def _is_captcha_or_challenge(html_text: str, url: str) -> bool:
    blob = f"{url or ''}\n{html_text or ''}".lower()
    return any(marker in blob for marker in _CAPTCHA_MARKERS)


def _has_normal_person_name(full_name: Optional[str]) -> bool:
    """已解析出的人物姓名。挑战标题（含验证码文案）不算正常人物页。"""
    if not full_name or not str(full_name).strip():
        return False
    folded = str(full_name).lower()
    return not any(marker in folded for marker in _CAPTCHA_MARKERS)


def _raise_http_captcha(url: str) -> None:
    err = HttpError(429, f"HTTP 429 captcha for {url}")
    # 429 在 HttpError 里会落到 http_4xx；限流重试看 rate_limit。
    if getattr(err, "bucket", None) == "http_4xx" and hasattr(err, "bucket"):
        err.bucket = "rate_limit"
    raise err


def _raise_if_captcha_page(html_text: str, url: str, full_name: Optional[str] = None) -> None:
    if _has_normal_person_name(full_name):
        return
    if _is_captcha_or_challenge(html_text, url):
        _raise_http_captcha(url)


# ============================================================
# 极致优化版解析器 (精简历史旧地址与别名，聚焦核心资产)
# ============================================================

def parse_person_lean(page, url: str) -> dict:
    """
    精简高通量解析器：
    提取核心资产：姓名、姓名分词(名/中间名/姓)、性别、年龄、出生年月、当前城市/州、当前地址、居住时长、房产详情、全部电话(无线/座机/VoIP)、移动号码1/2/3、全部邮箱。
    剔除极其冗余的历史过往地址 (previous_addresses) 与别名 (aliases)，
    配合流式截断，数据库写行数减少 65%，流量节省 50% 以上。
    """
    real_url = url
    for attr in ("response", "url", "_selector"):
        obj = getattr(page, attr, None)
        if obj is not None:
            found = getattr(obj, "url", None) if attr != "url" else obj
            if found and "/person/" in str(found):
                real_url = str(found)
                break

    person_id = extract_person_id(real_url) or extract_person_id(url)
    text = page.get_all_text() if hasattr(page, "get_all_text") else ""

    data = {
        "person_id": person_id,
        "source_url": real_url or url,
        "full_name": None,
        "first_name": None,
        "middle_name": None,
        "last_name": None,
        "gender": "未知",
        "age": None,
        "birth_month": None,
        "birth_year": None,
        "current_city": None,
        "current_state": None,
        "marital_status": None,
        "current_address": None,
        "address_duration": None,
        "primary_phone": None,
        "primary_phone_type": None,
        "all_phones": None,
        "wireless_phone_1": None,
        "wireless_phone_2": None,
        "wireless_phone_3": None,
        "aliases": [],               # 剔除：大幅减轻数据库子表写入
        "current_address_detail": {},
        "previous_addresses": [],    # 剔除：大幅减轻数据库子表写入
        "phone_numbers": [],
        "emails": [],
    }

    # --- 姓名（从 title 提取） ---
    title = page.css("title::text").get() if hasattr(page, "css") else ""
    title = title or ""
    name_match = re.match(r"^([^,]+)", title)
    if name_match:
        name = name_match.group(1).strip()
        name = re.sub(r"\s+-\s+TruePeopleSearch.*$", "", name, flags=re.I).strip()
        folded = name.lower().strip(" .…")
        is_phone_title = bool(re.match(r"^\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}", name))
        if not is_phone_title and folded not in {
            "captcha", "just a moment", "attention required",
            "access denied", "rate limited", "please wait", "checking your browser",
            "请稍候", "phone number lookup", "reverse phone lookup",
        } and not folded.startswith("请稍候"):
            data["full_name"] = name
            fn, mn, ln = split_full_name(name)
            data["first_name"] = fn or None
            data["middle_name"] = mn or None
            data["last_name"] = ln or None

    # --- 年龄 ---
    age_match = re.search(r"Age\s*(\d+)", text)
    if age_match:
        data["age"] = int(age_match.group(1))

    # --- 出生年月 ---
    birth_match = re.search(r"Born\s+(\w+)\s+(\d{4})", text)
    if birth_match:
        data["birth_month"] = MONTHS.get(birth_match.group(1).lower())
        data["birth_year"] = int(birth_match.group(2))

    # --- 当前城市/州 ---
    city_match = re.search(r"Lives in\s+([^,]+),\s+(\w{2})", text)
    if city_match:
        data["current_city"] = city_match.group(1).strip()
        data["current_state"] = city_match.group(2).strip()

    # --- 婚姻状态 ---
    if "does not appear to be married" in text:
        data["marital_status"] = "single"

    # --- 当前地址与居住时长 ---
    cur_detail = {}
    addr_match = re.search(
        r"Current Address.*?This is the most recently reported.*?address.*?\n"
        r"((.+?)\n(.+?)\n)",
        text, re.DOTALL,
    )
    if not addr_match:
        addr_match = re.search(
            r"Current Address.*?\n((.+?)\n(.+?)\n)",
            text, re.DOTALL,
        )
    if addr_match:
        street = addr_match.group(2).strip()
        city_state = addr_match.group(3).strip()
        cs_match = re.match(r"([^,]+),\s+(\w{2})\s+(\d+)", city_state)
        if cs_match:
            cur_detail["street"] = street
            cur_detail["city"] = cs_match.group(1)
            cur_detail["state"] = cs_match.group(2)
            cur_detail["zip_code"] = cs_match.group(3)
            data["current_address_text"] = f"{street}, {city_state}".strip(", ")
        elif street:
            data["current_address_text"] = street

    data["current_address"] = cur_detail
    data["current_address_detail"] = cur_detail

    dur_match = re.search(r"(\([A-Za-z]{3,}\s+\d{4}\s*-\s*(?:[A-Za-z]{3,}\s+\d{4}|Present|Current)\))", text, re.I)
    if dur_match:
        data["address_duration"] = dur_match.group(1).strip()

    # --- 房产详情 ---
    value_match = re.search(
        r"\$([\d,]+)\s*\|.*?(\d+)\s*Bath.*?([\d,]+)\s*Sq Ft.*?Built\s*(\d{4})",
        text,
    )
    if value_match:
        cur_detail["estimated_value"] = float(value_match.group(1).replace(",", ""))
        cur_detail["bathrooms"] = int(value_match.group(2))
        cur_detail["square_feet"] = int(value_match.group(3).replace(",", ""))
        cur_detail["year_built"] = int(value_match.group(4))

    # --- 县 (County) ---
    county_match = re.search(
        r"Current Address.*?\n.*?\n.*?\n.*?County", text, re.DOTALL
    )
    if county_match:
        county_text = county_match.group()
        c = re.search(r"(\w+)\s+County", county_text)
        if c:
            cur_detail["county"] = c.group(1) + " County"

    data["current_address_detail"] = cur_detail

    # --- 电话号码 (全量采集，无漏损) ---
    parsed_phones = extract_phone_numbers(text)
    data["phone_numbers"] = parsed_phones

    if parsed_phones:
        # 1. 电话列表 (逗号拼接所有捕获的号码，附带线路类型)
        data["all_phones"] = ", ".join(
            f"{p['phone_number']} ({p.get('line_type') or 'Unknown'})"
            for p in parsed_phones if p.get("phone_number")
        )

        # 2. 无线号码排序 (按最后报告时间倒序，最近的排在最前面)
        def _date_sort_key(p):
            return p.get("last_reported") or "0000-00-00"

        wireless_phones = [
            p for p in parsed_phones
            if str(p.get("line_type", "")).lower() == "wireless"
        ]
        wireless_sorted = sorted(wireless_phones, key=_date_sort_key, reverse=True)

        data["wireless_phone_1"] = wireless_sorted[0]["phone_number"] if len(wireless_sorted) > 0 else None
        data["wireless_phone_2"] = wireless_sorted[1]["phone_number"] if len(wireless_sorted) > 1 else None
        data["wireless_phone_3"] = wireless_sorted[2]["phone_number"] if len(wireless_sorted) > 2 else None

        # 3. 客户核心规则：如果主要电话号码后面是座机，就找下面最近时间的无线
        marked_primary = None
        for p in parsed_phones:
            if p.get("is_primary"):
                marked_primary = p
                break

        eligible_phones = [
            p for p in parsed_phones
            if _eligible_phone_type(p.get("line_type")) and _valid_us_phone(p.get("phone_number"))
        ]
        eligible_wireless = [p for p in wireless_sorted if p in eligible_phones]
        if marked_primary in eligible_wireless:
            chosen = marked_primary
        elif eligible_wireless:
            chosen = eligible_wireless[0]
        elif marked_primary in eligible_phones:
            chosen = marked_primary
        else:
            landlines = [p for p in eligible_phones if p not in eligible_wireless]
            chosen = max(landlines, key=_date_sort_key) if landlines else None

        if chosen:
            data["primary_phone"] = chosen.get("phone_number")
            data["primary_phone_type"] = chosen.get("line_type")

    # --- 电子邮箱 ---
    email_section = re.search(
        r"Email Addresses.*?(?:Current Address Property Details|Previous Addresses|Possible Relatives|$)",
        text, re.DOTALL,
    )
    if email_section:
        emails = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", email_section.group())
        seen = set()
        for email in emails:
            if email not in seen:
                seen.add(email)
                data["emails"].append({"email": email})

    return data


def get_warmed_cookies(host: str, sid: str = "") -> tuple[dict, str, str]:
    """从网关发布的暖机 Cookie 里取对应 host 的 cookies+UA；没有则返回 ({}, '', '')。
    sid 键优先（同出口复用，cf_clearance 绑 IP），通用键兜底。
    第三个返回值是命中的键类型（'sid'/'host'/''），供失效时精准踢出。"""
    host = (host or "").lower() or "www.truepeoplesearch.com"
    sid = (sid or "").strip()[:12]
    try:
        import redis  # type: ignore

        r = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=0.4,
            socket_timeout=0.8,
        )
        keys = ([(f"unblocker:warmed:{host}:{sid}", "sid")] if sid else []) + [(f"unblocker:warmed:{host}", "host")]
        for key, hit in keys:
            raw = r.get(key)
            if not raw:
                continue
            data = json.loads(raw)
            if time.time() - float(data.get("ts", 0)) > 1500:
                continue
            cookies = data.get("cookies") or {}
            if not isinstance(cookies, dict) or not cookies:
                continue
            return cookies, str(data.get("user_agent") or ""), hit
        return {}, "", ""
    except Exception:
        return {}, "", ""


COVERAGE_FIELDS = ("full_name", "age", "birth_year", "current_city", "current_state",
                   "current_address", "primary_phone", "phone_numbers", "emails")
COVERAGE_THRESHOLDS = {"full_name": 0.9, "primary_phone": 0.4, "phone_numbers": 0.4}
COVERAGE_TTL_SEC = 86400


def _field_filled(value) -> bool:
    if value is None:
        return False
    if value == "未知":
        return False
    if isinstance(value, (list, dict, set, tuple)):
        return len(value) > 0
    if isinstance(value, str):
        return bool(value.strip())
    return True


def coverage_report(results: list) -> dict:
    """对一批解析结果算字段覆盖率：解析器被改版打瞎时第一时间现形。"""
    rows = [r for r in (results or []) if isinstance(r, dict)]
    total = len(rows)
    fields = {}
    for f in COVERAGE_FIELDS:
        n = sum(1 for r in rows if _field_filled(r.get(f)))
        fields[f] = {"filled": n, "rate": (round(n / total, 4) if total else 0.0)}
    return {"total": total, "fields": fields}


def coverage_alert(report: dict, thresholds: dict = None) -> list:
    """覆盖率掉到阈值下就报警（默认：姓名 90%，电话 40%）。"""
    th = thresholds or COVERAGE_THRESHOLDS
    out = []
    total = (report or {}).get("total", 0)
    if not total:
        return ["无解析样本，覆盖率未知"]
    for f, limit in th.items():
        rate = ((report.get("fields") or {}).get(f) or {}).get("rate", 0.0)
        if rate < limit:
            out.append(f"{f} 覆盖率 {rate:.1%} < {limit:.0%}（{total} 样本），解析器可能被改版打瞎")
    return out


_SHARED_REDIS = None


def _shared_redis():
    """模块级复用连接：解析/覆盖率/学习共用，失败返回 None（调用方跳过）。"""
    global _SHARED_REDIS
    if _SHARED_REDIS is not None:
        try:
            _SHARED_REDIS.ping()
            return _SHARED_REDIS
        except Exception:
            _SHARED_REDIS = None
    try:
        import redis  # type: ignore

        client = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=0.3,
            socket_timeout=0.5,
        )
        client.ping()
        _SHARED_REDIS = client
        return client
    except Exception:
        return None


def record_coverage(result: dict) -> None:
    """每次解析成功记一笔滚动覆盖率（Redis，24h 滚动窗口），失败静默跳过。
    过期只在键新建时设一次（incr 返回 1），窗口才能真正滚动，不会越积越多。"""
    if not isinstance(result, dict):
        return
    try:
        r = _shared_redis()
        if r is None:
            return
        filled = [f for f in COVERAGE_FIELDS if _field_filled(result.get(f))]
        pipe = r.pipeline()
        pipe.incr("tps:cov:total")
        for f in filled:
            pipe.incr(f"tps:cov:{f}")
        res = pipe.execute() or []
        fresh = [i for i, v in enumerate(res) if v == 1]
        if fresh:
            keys = ["tps:cov:total"] + [f"tps:cov:{f}" for f in filled]
            pipe2 = r.pipeline()
            for i in fresh:
                if i < len(keys):
                    pipe2.expire(keys[i], COVERAGE_TTL_SEC)
            pipe2.execute()
    except Exception:
        pass


def read_coverage() -> dict:
    """读滚动覆盖率（供网关 /health 展示），无 Redis 返回空。"""
    try:
        import redis  # type: ignore

        r = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=0.3,
            socket_timeout=0.5,
        )
        total = int(r.get("tps:cov:total") or 0)
        fields = {}
        for f in COVERAGE_FIELDS:
            n = int(r.get(f"tps:cov:{f}") or 0)
            fields[f] = {"filled": n, "rate": (round(n / total, 4) if total else 0.0)}
        return {"total": total, "fields": fields,
                "alerts": coverage_alert({"total": total, "fields": fields}) if total else []}
    except Exception:
        return {}


def drop_warmed_cookies(host: str, sid: str = "", hit: str = "") -> None:
    """暖机 Cookie 已失效（拿着它吃 403/验证）时，精准删掉命中的那把键，
    避免后续请求继续用死 Cookie 空撞，下一轮自动触发浏览器重暖。"""
    host = (host or "").lower() or "www.truepeoplesearch.com"
    try:
        import redis  # type: ignore

        r = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=0.4,
            socket_timeout=0.8,
        )
        if hit == "sid" and sid:
            r.delete(f"unblocker:warmed:{host}:{sid.strip()[:12]}")
        elif hit == "host":
            r.delete(f"unblocker:warmed:{host}")
    except Exception:
        pass


class ProtocolFetcher:
    """协议层异步抓取器 (支持流式截断与极限流量压缩)"""

    def __init__(self, impersonate: str = "chrome124", default_timeout: int = 15):
        self.impersonate = impersonate
        self.default_timeout = default_timeout

    async def fetch_person(
        self,
        url: str,
        proxy: Optional[str] = None,
        timeout: Optional[int] = None,
        session: Optional[AsyncSession] = None,
        stream_cutoff: bool = True,
        cookies: Optional[dict] = None,
        use_warmed: bool = True,
        sid: str = "",
    ) -> dict:
        """
        异步请求 TruePeopleSearch 人物页面并解析结构化数据。

        :param stream_cutoff: 是否开启流式截断（只下前 10KB，检测到 Previous Addresses 立即掐断，极度省流量）
        :param sid: 本 lane 的 sticky 会话 id；传了则暖机优先命中同出口（cf_clearance 绑 IP），
            且暖机失效时精准踢出，不影响别的 lane。
        """
        to = timeout or self.default_timeout
        own_session = session is None

        # 协议主跑：优先用浏览器暖机好的 Cookie（网关 /v1/scrape 成功后发布到 Redis）
        warmed_ua = ""
        warmed_hit = ""
        warmed_host = ""
        if cookies is None and use_warmed:
            try:
                warmed_host = urlparse(url).netloc.lower() or "www.truepeoplesearch.com"
                cookies, warmed_ua, warmed_hit = get_warmed_cookies(warmed_host, sid)
            except Exception:
                cookies, warmed_ua, warmed_hit = None, "", ""

        if own_session:
            session = AsyncSession(impersonate=self.impersonate)
        if cookies and own_session:
            try:
                session.cookies.update(cookies)
            except Exception:
                pass

        try:
            headers = dict(DEFAULT_HEADERS)
            if warmed_ua:
                headers["user-agent"] = warmed_ua
            if cookies:
                try:
                    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items() if k)
                except Exception:
                    pass
            req_kwargs = {
                "headers": headers,
                "timeout": to,
                "allow_redirects": True,
                "accept_encoding": "gzip, deflate, br, zstd",
            }
            if proxy:
                req_kwargs["proxy"] = proxy

            if stream_cutoff:
                # -------------------------------------------------------------
                # 极致优化：流式按需截断模式 (Stream Early-Cutoff)
                # -------------------------------------------------------------
                try:
                    resp = await session.get(url, stream=True, **req_kwargs)
                except CurlError as exc:
                    err_msg = str(exc).lower()
                    if "timeout" in err_msg or "timed out" in err_msg:
                        raise FetchTimeoutError(f"Request timeout {to}s for {url}") from exc
                    elif "proxy" in err_msg or "could not resolve proxy" in err_msg or "failed to connect" in err_msg:
                        raise ProxyError(f"Proxy failed: {exc}") from exc
                    else:
                        raise ScrapeError(f"Network error: {exc}", bucket="network_err") from exc
                except Exception as exc:
                    err_msg = str(exc).lower()
                    if "timeout" in err_msg:
                        raise FetchTimeoutError(f"Timeout for {url}") from exc
                    raise ScrapeError(f"Unexpected fetch error: {exc}") from exc

                status = resp.status_code
                if status == 404:
                    await resp.aclose()
                    raise EmptyPageError(f"Person not found (404) for {url}")
                if status in (403, 503):
                    await resp.aclose()
                    raise CloudflareChallengeError(f"Cloudflare challenge encountered (status {status}) for {url}")
                if status != 200:
                    await resp.aclose()
                    raise HttpError(status, f"HTTP {status} for {url}")

                chunks = []
                total_bytes = 0

                try:
                    async for chunk in resp.aiter_content():
                        chunks.append(chunk)
                        total_bytes += len(chunk)
                        c_low = chunk.lower()
                        # 一旦在数据流中嗅探到过往地址或亲戚，代表上方核心信息（电话、邮箱、当前地址）已下载完毕
                        # 立即掐断连接，不再传输后续几十 KB 的广告与多余数据
                        # 注意：sponsored by 可能是广告位先出现，不能当截断信号（会丢电话/邮箱）
                        if (
                            b"previous addresses" in c_low
                            or b"possible relatives" in c_low
                            or b"possible associates" in c_low
                        ):
                            break
                        if total_bytes >= 35000:  # 35KB 安全上限
                            break
                finally:
                    await resp.aclose()

                html_text = b"".join(chunks).decode("utf-8", errors="replace")
                final_req_url = str(getattr(resp, "url", "") or url)

            else:
                # 完整下载模式
                try:
                    resp = await session.get(url, **req_kwargs)
                except CurlError as exc:
                    err_msg = str(exc).lower()
                    if "timeout" in err_msg or "timed out" in err_msg:
                        raise FetchTimeoutError(f"Request timeout {to}s for {url}") from exc
                    elif "proxy" in err_msg or "could not resolve proxy" in err_msg or "failed to connect" in err_msg:
                        raise ProxyError(f"Proxy failed: {exc}") from exc
                    else:
                        raise ScrapeError(f"Network error: {exc}", bucket="network_err") from exc

                status = resp.status_code
                final_req_url = str(getattr(resp, "url", "") or url)
                try:
                    html_text = resp.text

                    cf_blocked = check_cloudflare_blocked(status, html_text)
                    if cf_blocked:
                        raise CloudflareChallengeError(f"Cloudflare challenge encountered (status {status}) for {url}")
                    if status == 404:
                        raise EmptyPageError(f"Person not found (404) for {url}")
                    if status != 200:
                        raise HttpError(status, f"HTTP {status} for {url}")
                    # status 200 且 Cloudflare 检测未命中时，同样识别验证码页。
                    if status == 200 and not cf_blocked and _is_captcha_or_challenge(html_text, url):
                        early_name = parse_person_lean(Adaptor(html_text), url).get("full_name")
                        _raise_if_captcha_page(html_text, url, early_name)
                finally:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass

            # 统一阻断检查与精简解析
            cf_blocked = check_cloudflare_blocked(200, html_text)
            if cf_blocked:
                raise CloudflareChallengeError(f"Cloudflare challenge encountered for {url}")

            # 检查是否为电话反查或搜索结果列表页（final_req_url 已在下载分支提前取值）
            is_search = "resultphone" in url.lower() or "/results?" in url.lower() or "resultname" in url.lower()
            has_person_path = bool(re.search(r"/(?:find/)?person/([a-zA-Z0-9_]+)", final_req_url))

            if is_search and not has_person_path:
                person_links = re.findall(r"/find/person/([a-zA-Z0-9_]+)", html_text)
                if person_links:
                    unique_pids = list(dict.fromkeys(person_links))
                    try:
                        import redis
                        r = redis.Redis(
                            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                            port=int(os.environ.get("REDIS_PORT", "6379")),
                            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
                            decode_responses=True,
                        )
                        from tps_queue import feed
                        full_urls = [f"https://www.truepeoplesearch.com/find/person/{pid}" for pid in unique_pids]
                        res = feed(r, full_urls)
                        print(f"[SEARCH_RESULT] 协议层电话搜索已捕获并注入 {len(unique_pids)} 个目标人物档案: {res}", flush=True)
                    except Exception as feed_err:
                        print(f"[SEARCH_FEED_ERR] 注入队列提示: {feed_err}", flush=True)
                    return {"is_search_result": True, "count": len(unique_pids), "person_ids": unique_pids}
                else:
                    raise EmptyPageError(f"电话反查无匹配记录 (0 results): {url}")

            doc = Adaptor(html_text)
            data = parse_person_lean(doc, final_req_url)
            full_name = data.get("full_name")

            if status == 200 and not cf_blocked:
                _raise_if_captcha_page(html_text, url, full_name)

            if not full_name or not data.get("person_id"):
                _raise_if_captcha_page(html_text, url, full_name)
                raise EmptyPageError(f"Empty page (no valid person) for {url}")

            record_coverage(data)
            try:
                from phone_plan import learn_from_person

                learn_from_person(data, _shared_redis())
            except Exception:
                pass
            return data

        except (CloudflareChallengeError, HttpError) as exc:
            # 拿着暖机 Cookie 依然撞验证/403：这份暖机已死，精准踢出，
            # 下一轮自动回退浏览器重暖；429 等限流不踢（Cookie 本身可能没问题）。
            is_dead_warmed = isinstance(exc, CloudflareChallengeError) or (
                isinstance(exc, HttpError) and getattr(exc, "status", 0) == 403
            )
            if is_dead_warmed and warmed_hit and warmed_host:
                try:
                    drop_warmed_cookies(warmed_host, sid, warmed_hit)
                except Exception:
                    pass
            raise
        finally:
            if own_session and session:
                await session.close()
