#!/usr/bin/env python3
"""
TruePeopleSearch -> TiDB 全流程抓取入库脚本

依赖安装：
  pip install "scrapling[fetchers]" mysql-connector-python redis

使用：
  python3 scrape_to_tidb.py --url "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l2l8n60"
  python3 scrape_to_tidb.py --batch urls.txt          # 批量模式，每行一个 URL
"""

import os
import re
import json
import hashlib
import argparse
from typing import Optional
from datetime import datetime

import mysql.connector
try:
    from scrapling.fetchers import StealthyFetcher
except Exception:
    StealthyFetcher = None


# ============================================================
# TiDB / MySQL 连接配置。只连接显式配置的单一目标，不猜测密码或端口。
# ============================================================
TIDB_CONFIG = {
    "host": os.environ.get("TPS_DB_HOST") or os.environ.get("TIDB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("TPS_DB_PORT") or os.environ.get("TIDB_PORT", 4000)),
    "user": os.environ.get("TPS_DB_USER") or os.environ.get("TIDB_USER", "root"),
    "password": os.environ.get("TPS_DB_PASSWORD", os.environ.get("TIDB_PASSWORD", "")),
    "database": os.environ.get("TPS_DB_NAME") or os.environ.get("TIDB_DATABASE", "people_search"),
    "autocommit": False,
}

_redis_singleton = None


def _queue_redis():
    """搜索结果回灌用的 Redis 单例：每页新建连接会泄 fd。"""
    global _redis_singleton
    if _redis_singleton is None:
        import redis
        _redis_singleton = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
        )
    return _redis_singleton


def db_target_fingerprint() -> str:
    """Non-secret identity shared by worker/ingester to prevent split DB targets."""
    target = {name: TIDB_CONFIG[name] for name in ("host", "port", "user", "database")}
    return hashlib.sha256(json.dumps(target, sort_keys=True).encode("utf-8")).hexdigest()


def get_db():
    """仅连接当前配置的数据库；错误交给调用方处理。"""
    return mysql.connector.connect(**TIDB_CONFIG)


class ScrapeError(Exception):
    """抓取/入库失败基类，worker 按 bucket 分桶。"""

    bucket = "error"

    def __init__(self, message: str = "", bucket: Optional[str] = None):
        super().__init__(message)
        if bucket is not None:
            self.bucket = bucket


class HttpError(ScrapeError):
    """HTTP 非 200。属性 status / bucket（4xx -> http_4xx，5xx -> http_5xx）。"""

    bucket = "http_4xx"

    def __init__(self, status, message: str = None, bucket: Optional[str] = None):
        self.status = status
        if bucket is None:
            try:
                code = int(status)
            except (TypeError, ValueError):
                code = 0
            bucket = "http_4xx" if code < 500 else "http_5xx"
        super().__init__(message or f"HTTP {status}", bucket=bucket)


class EmptyPageError(ScrapeError):
    """解析结果缺少 full_name，视为空页。"""

    bucket = "empty"

    def __init__(self, message: str = "empty page", bucket: str = "empty"):
        super().__init__(message, bucket=bucket)


_CAPTCHA_MARKERS = (
    "internalcaptcha",
    "just a moment",
    "cf-challenge",
    "attention required",
    "请稍候",
    "cf-turnstile",
)


def is_captcha_document(url: str, html: str) -> bool:
    """URL 或 HTML 命中验证码/挑战页标记（大小写不敏感）。"""
    blob = f"{url or ''}\n{html or ''}".lower()
    return any(marker in blob for marker in _CAPTCHA_MARKERS)


class FetchTimeoutError(ScrapeError):
    """浏览器/Cloudflare 等待超时。"""

    bucket = "cf_fail"

    def __init__(self, message: str = "fetch timeout", bucket: str = "cf_fail"):
        super().__init__(message, bucket=bucket)


FETCH_TIMEOUT_MS = int(float(__import__("os").environ.get("TPS_FETCH_TIMEOUT_MS", "60")) * 1000)

STEALTH_INIT_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stealth_init.js")

# Referer 轮换：全站 100% Google referer 是最显眼的机器人特征之一。
# 真实流量主体是站内跳转（搜索页 -> 人物页），搜索引擎只占一小部分。
# TPS_REFERER_MODE=google 恢复旧行为；=none 则不带 referer。
_REFERER_INTERNAL = (
    "https://www.truepeoplesearch.com/",
    "https://www.truepeoplesearch.com/find/person/",
    "https://www.truepeoplesearch.com/results?",
)
_REFERER_SEARCH = (
    "https://www.google.com/",
    "https://www.bing.com/",
    "https://search.yahoo.com/",
)


def pick_referer(url: str = "") -> str:
    """按权重挑 referer：站内 70% / 搜索 20% / 直连(空) 10%。"""
    import random as _random
    mode = (os.environ.get("TPS_REFERER_MODE") or "rotate").strip().lower()
    if mode == "google":
        return "https://www.google.com/"
    if mode == "none":
        return ""
    roll = _random.random()
    if roll < 0.70:
        base = _random.choice(_REFERER_INTERNAL)
        # 人物页的上一跳多半是站内搜索/人物页，用固定前缀即可，真拼 URL 反而假
        return base
    if roll < 0.90:
        return _random.choice(_REFERER_SEARCH)
    return ""


def parse_jitter_ms() -> tuple:
    """TPS_FETCH_JITTER_MS="200,800" -> (200, 800)。配 "0,0" 关闭。"""
    raw = (os.environ.get("TPS_FETCH_JITTER_MS") or "200,800").strip()
    try:
        lo_s, _, hi_s = raw.partition(",")
        lo, hi = int(lo_s or 0), int(hi_s or lo_s or 0)
    except (TypeError, ValueError):
        return (200, 800)
    lo, hi = max(0, lo), max(0, hi)
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)


def _env_flag(name: str, default: bool) -> bool:
    import os as _os
    raw = _os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off", "none")


# TPS_REQUIRE_PHONE=0 时允许无人名下有效电话的人物也入库（面板先有数）；
# 默认 1 保持电话门，但查询号兜底 + VoIP/无类型回收后通过率会明显上升。
REQUIRE_PHONE = _env_flag("TPS_REQUIRE_PHONE", True)
ACCEPT_VOIP = _env_flag("TPS_ACCEPT_VOIP", True)
ACCEPT_UNKNOWN_TYPE = _env_flag("TPS_ACCEPT_UNKNOWN_TYPE", True)


def _phone_digits_from_url(url: str) -> str:
    """从 /find/phone/xxxx 或 resultphone=xxxx 中提取查询的 10/11 位号码。"""
    if not url:
        return ""
    m = re.search(r"/find/phone/(\d{7,11})", str(url))
    if m:
        digits = re.sub(r"\D", "", m.group(1))
    else:
        m2 = re.search(r"resultphone=(\d{7,11})", str(url))
        digits = re.sub(r"\D", "", m2.group(1)) if m2 else ""
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _format_us_phone(digits10: str) -> str:
    d = re.sub(r"\D", "", digits10 or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        return ""
    return f"({d[:3]}) {d[3:6]}-{d[6:]}"


def _synthesize_queried_phone(url: str) -> Optional[dict]:
    """电话反查页重定向到人物页时：查询号本身就是一条有效号码兜底。"""
    digits = _phone_digits_from_url(url)
    if not digits:
        return None
    formatted = _format_us_phone(digits)
    if not _valid_us_phone(formatted):
        return None
    return {
        "phone_number": formatted,
        "line_type": None,
        "carrier": None,
        "last_reported": None,
        "is_primary": False,
        "queried": True,
    }


def extract_person_id(url: str) -> str:
    """从 URL 提取 person_id，例如 /find/person/px82l44nur68u2l8n60 -> px82l44nur68u2l8n60"""
    if not url:
        return ""
    match = re.search(r"/(?:find/)?person/([a-zA-Z0-9_]+)", str(url))
    return match.group(1) if match else ""


MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def parse_date(date_str: Optional[str]) -> Optional[str]:
    """解析 'Aug 2026' 或 'August 2026' 格式为 '2026-08-01'"""
    if not date_str:
        return None
    match = re.search(r"([A-Za-z]+)\s+(\d{4})", str(date_str).strip())
    if match:
        m_str = match.group(1).lower()
        year = match.group(2)
        month = MONTHS.get(m_str)
        if month:
            return f"{year}-{month:02d}-01"
    return None


def split_full_name(full_name: str) -> tuple:
    """
    拆分全名为 (名, 中间名, 姓)
    Western public record convention:
    - 1 token: (token, '', '')
    - 2 tokens: (tokens[0], '', tokens[1])
    - 3 tokens: (tokens[0], tokens[1], tokens[2])
    - >3 tokens: (tokens[0], ' '.join(tokens[1:-1]), tokens[-1])
    """
    if not full_name:
        return "", "", ""
    clean = re.sub(r",.*$", "", str(full_name)).strip()
    clean = re.sub(r"\s+-\s+TruePeopleSearch.*$", "", clean, flags=re.I).strip()
    clean = re.sub(r"\s*\(.*?\)\s*", " ", clean).strip()
    parts = clean.split()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def extract_phone_numbers(text: str) -> list:
    """
    仅提取人物 Phone Numbers 分节中的电话，避免把 Businesses 等区的号码归给人物。
    """
    if not text:
        return []

    heading = re.search(r"(?im)^[ \t]*Phone Numbers?(?:[ \t]*\(\d+\))?[ \t]*$", text)
    if not heading:
        # 标题格式漂移兜底：全文扫描有效号码，类型记 None（由 ACCEPT_UNKNOWN_TYPE 开关决定是否可用）。
        return _fallback_scan_phones(text)
    after_heading = text[heading.end():]
    next_section = re.search(
        r"(?im)^[ \t]*(?:Email Addresses|Current Address Property Details|Previous Addresses|"
        r"Possible Relatives|Possible Associates|Businesses|Associated Names|Online Profiles)\b",
        after_heading,
    )
    section_text = after_heading[:next_section.start()] if next_section else after_heading

    phone_regex = re.compile(r"(?:\+?1[-.\s]*)?\(?([2-9]\d{2})\)?[-.\s]*([2-9]\d{2})[-.\s]*(\d{4})")
    matches = list(phone_regex.finditer(section_text))
    if not matches:
        # 分节内无号码时同样走全文兜底，避免标题定位偏了就整页丢号。
        return _fallback_scan_phones(text)

    phones = []
    seen_numbers = set()
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        block = section_text[start:end].strip()

        formatted_num = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
        if formatted_num in seen_numbers:
            continue
        seen_numbers.add(formatted_num)

        # 1. 类型只能来自号码同一行的显式标签（或独立的下一行）。
        # 不能把运营商名称如 Verizon Wireless 误当成号码类型。
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        number_line = lines[0] if lines else ""
        type_match = re.search(r"[-–—|]\s*(Wireless|Landline(?:/Services)?|VoIP)\b", number_line, re.IGNORECASE)
        if not type_match and len(lines) > 1:
            type_match = re.fullmatch(r"(Wireless|Landline(?:/Services)?|VoIP)", lines[1], re.IGNORECASE)
        line_type = None
        if type_match:
            lt = type_match.group(1).lower()
            if lt == "voip":
                line_type = "Voip"
            elif lt == "wireless":
                line_type = "Wireless"
            elif "landline" in lt:
                line_type = "Landline/Services" if "services" in lt else "Landline"

        # 2. 是否主号
        is_primary = bool(re.search(r"Possible\s+Primary", block, re.IGNORECASE))

        # 3. 最后报告日期
        date_match = re.search(r"Last\s+reported\s+([A-Za-z]+)\s+(\d{4})", block, re.IGNORECASE)
        last_reported = None
        if date_match:
            last_reported = parse_date(f"{date_match.group(1)} {date_match.group(2)}")

        # 4. 运营商提取（逐行过滤，保留完整运营商名称，避免误伤与残余词干扰）
        carrier_candidates = []
        for line in lines:
            if phone_regex.search(line):
                rem = phone_regex.sub("", line)
                rem = re.sub(r"\b(Wireless|Landline(?:/Services)?|VoIP|Voip)\b", "", rem, flags=re.I)
                rem = re.sub(r"Possible\s+Primary", "", rem, flags=re.I)
                rem = re.sub(r"[-–—|\s]+", " ", rem).strip()
                if rem and rem.lower() not in ("phone", "phones", "primary phone") and len(rem) > 2:
                    carrier_candidates.append(rem)
                continue
            if re.match(r"^Phone Numbers?$", line, re.I):
                continue
            if re.search(r"Last\s+reported", line, re.I):
                continue
            norm = re.sub(r"\b(Wireless|Landline(?:/Services)?|VoIP|Voip)\b", "", line, flags=re.I)
            norm = re.sub(r"Possible\s+Primary", "", norm, flags=re.I)
            norm = re.sub(r"[-–—|\s]+", " ", norm).strip().lower()
            if not norm or norm in ("possible primary", "possible primary phone", "primary phone", "phone", "phones"):
                continue

            clean_cand = re.sub(r"^[-–—|\s]+|[-–—|\s]+$", "", line).strip()
            if clean_cand and not clean_cand.startswith("(") and len(clean_cand) > 1:
                carrier_candidates.append(clean_cand)

        carrier = carrier_candidates[0] if carrier_candidates else None
        if carrier:
            carrier = re.sub(r"\s+", " ", carrier).strip()

        phones.append({
            "phone_number": formatted_num,
            "line_type": line_type,
            "carrier": carrier,
            "last_reported": last_reported,
            "is_primary": is_primary,
        })

    return phones


def _fallback_scan_phones(text: str) -> list:
    """全文兜底扫描：标题缺失/漂移时回收有效号码，line_type 记 None。"""
    if not text:
        return []
    phone_regex = re.compile(r"(?:\+?1[-.\s]*)?\(?([2-9]\d{2})\)?[-.\s]*([2-9]\d{2})[-.\s]*(\d{4})")
    phones = []
    seen = set()
    for m in phone_regex.finditer(text):
        formatted = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
        if formatted in seen or not _valid_us_phone(formatted):
            continue
        seen.add(formatted)
        phones.append({
            "phone_number": formatted,
            "line_type": None,
            "carrier": None,
            "last_reported": None,
            "is_primary": False,
        })
    return phones


def _resolve_person_url(page, url: str) -> str:
    """提取重定向或渲染后的真实页面 URL"""
    for attr in ("response", "url", "_selector"):
        obj = getattr(page, attr, None)
        if obj is not None:
            found = getattr(obj, "url", None) if attr != "url" else obj
            if found and "/person/" in str(found):
                return str(found)
    return url or ""


# ============================================================
# 页面解析 — 从 Scrapling 抓取结果提取结构化数据
# ============================================================

def parse_person(page, url: str) -> dict:
    """解析 TruePeopleSearch 人物页面，提取所有结构化字段"""
    real_url = _resolve_person_url(page, url)
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
        "current_address": {},
        "current_address_text": None,
        "address_duration": None,
        "primary_phone": None,
        "primary_phone_type": None,
        "all_phones": None,
        "wireless_phone_1": None,
        "wireless_phone_2": None,
        "wireless_phone_3": None,
        "aliases": [],
        "current_address_detail": {},
        "previous_addresses": [],
        "phone_numbers": [],
        "emails": [],
    }

    # --- 姓名（从 title 提取） ---
    title = page.css("title::text").get() if hasattr(page, "css") else ""
    title = title or ""
    if is_captcha_document(url, text or title) or is_captcha_document(url, title):
        return data

    name_match = re.match(r"^([^,]+)", title)
    if name_match:
        name = name_match.group(1).strip()
        name = re.sub(r"\s+-\s+TruePeopleSearch.*$", "", name, flags=re.I).strip()
        folded = name.lower().strip(" .…")
        # 排除纯电话格式的标题 (如 (201) 200-0000)
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

    # --- 别名 ---
    alias_section = re.search(r"Also Seen As.*?(?:Current Address|Phone Numbers)", text, re.DOTALL)
    if alias_section:
        alias_lines = [
            line.strip().rstrip(",") for line in alias_section.group().splitlines()
            if line.strip() and not line.strip().startswith("Also Seen As")
        ]
        seen = set()
        for alias in alias_lines:
            if alias and alias != data["full_name"] and alias not in seen and len(alias) > 3:
                seen.add(alias)
                data["aliases"].append({"alias_name": alias})

    # --- 当前地址与居住时长 ---
    cur_detail = {}
    addr_match = re.search(
        r"Current Address.*?This is the most recently reported.*?address.*?\n"
        r"((.+?)\n(.+?)\n)",
        text, re.DOTALL,
    )
    if not addr_match:
        addr_match = re.search(r"Current Address.*?\n((.+?)\n(.+?)\n)", text, re.DOTALL)

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

    # 居住时长例如 (Jan 2012 - Aug 2026)
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

    # --- 县 ---
    county_match = re.search(
        r"Current Address.*?\n.*?\n.*?\n.*?County", text, re.DOTALL
    )
    if county_match:
        county_text = county_match.group()
        c = re.search(r"(\w+)\s+County", county_text)
        if c:
            cur_detail["county"] = c.group(1) + " County"

    data["current_address"] = cur_detail
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

    # --- 邮箱 ---
    email_section = re.search(
        r"Email Addresses.*?Current Address Property Details", text, re.DOTALL
    )
    if email_section:
        emails = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", email_section.group())
        seen = set()
        for email in emails:
            if email not in seen:
                seen.add(email)
                data["emails"].append({"email": email})

    # --- 过往地址 ---
    prev_section = re.search(
        r"Previous Addresses.*?Possible Relatives", text, re.DOTALL
    )
    if prev_section:
        prev_text = prev_section.group()
        prev_addrs = re.findall(
            r"((?:\d+\s+)?[^\n]+\n[^,]+,\s+\w{2}\s+\d{5}\n\w+\s+County)",
            prev_text,
        )
        for addr_block in prev_addrs:
            lines = addr_block.strip().split("\n")
            street = lines[0].strip() if len(lines) > 0 else None
            cs = lines[1].strip() if len(lines) > 1 else ""
            cs_match = re.match(r"([^,]+),\s+(\w{2})\s+(\d+)", cs)
            county = lines[2].strip() if len(lines) > 2 else None
            if cs_match:
                data["previous_addresses"].append({
                    "street": street,
                    "city": cs_match.group(1),
                    "state": cs_match.group(2),
                    "zip_code": cs_match.group(3),
                    "county": county,
                })

    return data


# ============================================================
# TiDB 写入
# ============================================================

_PERSON_CORE_FIELDS = (
    "person_id", "full_name", "first_name", "middle_name", "last_name",
    "gender", "age", "birth_month", "birth_year",
    "primary_phone", "primary_phone_type",
    "current_address", "address_duration",
    "all_phones", "wireless_phone_1", "wireless_phone_2", "wireless_phone_3",
    "current_city", "current_state", "marital_status", "source_url",
)
_COUNT_FIELDS = (
    "phone_count", "email_count", "alias_count",
    "relative_count", "associate_count", "prev_addr_count",
)


def compute_content_hash(data: dict) -> str:
    """对关键字段做稳定 JSON 后 sha256，便于跳过未变更子表写入。"""
    ca = data.get("current_address_detail") if isinstance(data.get("current_address_detail"), dict) else (data.get("current_address") if isinstance(data.get("current_address"), dict) else {})
    payload = {
        "person_id": data.get("person_id"),
        "full_name": data.get("full_name"),
        "age": data.get("age"),
        "birth_month": data.get("birth_month"),
        "birth_year": data.get("birth_year"),
        "current_city": data.get("current_city"),
        "current_state": data.get("current_state"),
        "marital_status": data.get("marital_status"),
        "aliases": sorted(
            a.get("alias_name") or "" for a in (data.get("aliases") or [])
        ),
        "current_address": {
            k: ca.get(k)
            for k in (
                "street", "unit", "city", "state", "zip_code", "county",
                "estimated_value", "bathrooms", "square_feet", "year_built",
                "hoa_fee_monthly",
            )
        },
        "previous_addresses": sorted(
            [
                {
                    "street": a.get("street"),
                    "city": a.get("city"),
                    "state": a.get("state"),
                    "zip_code": a.get("zip_code"),
                    "county": a.get("county"),
                }
                for a in (data.get("previous_addresses") or [])
            ],
            key=lambda x: (x.get("street") or "", x.get("zip_code") or ""),
        ),
        "phone_numbers": sorted(
            [
                {
                    "phone_number": p.get("phone_number"),
                    "line_type": p.get("line_type"),
                    "carrier": p.get("carrier"),
                    "last_reported": p.get("last_reported"),
                    "is_primary": p.get("is_primary"),
                }
                for p in (data.get("phone_numbers") or [])
            ],
            key=lambda x: x.get("phone_number") or "",
        ),
        "emails": sorted(e.get("email") or "" for e in (data.get("emails") or [])),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _child_counts(data: dict) -> dict:
    return {
        "phone_count": len(data.get("phone_numbers") or []),
        "email_count": len(data.get("emails") or []),
        "alias_count": len(data.get("aliases") or []),
        "relative_count": 0,
        "associate_count": 0,
        "prev_addr_count": len(data.get("previous_addresses") or []),
    }


def _eligible_phone_type(value: object) -> bool:
    norm = str(value or "").strip().lower()
    if norm in {"wireless", "landline", "landline/services"}:
        return True
    if norm in {"voip", "voice over ip"} and ACCEPT_VOIP:
        return True
    # 解析器兜底（全文扫描 / 查询号合成）的号码没有类型标注；
    # TPS_ACCEPT_UNKNOWN_TYPE=0 可关掉这条回收。
    if (not norm or norm in {"unknown", "none", "null"}) and ACCEPT_UNKNOWN_TYPE:
        return True
    return False


def _valid_us_phone(value: object) -> bool:
    if not value:
        return False
    raw = str(value)
    if not re.fullmatch(r"[0-9+() .-]+", raw):
        return False
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return len(digits) == 10 and digits[0] >= "2" and digits[3] >= "2" and len(set(digits)) > 1


def has_usable_phone(data: dict) -> bool:
    """Wireless/Landline 必过；VoIP 与无类型号由 TPS_ACCEPT_VOIP/_UNKNOWN_TYPE 开关控制。"""
    if _eligible_phone_type(data.get("primary_phone_type")) and _valid_us_phone(data.get("primary_phone")):
        return True
    for name in ("wireless_phone_1", "wireless_phone_2", "wireless_phone_3"):
        if _valid_us_phone(data.get(name)):
            return True
    for entry in data.get("phone_numbers") or []:
        if isinstance(entry, dict) and _eligible_phone_type(entry.get("line_type")) and _valid_us_phone(entry.get("phone_number")):
            return True
    return False


def _persons_columns(cursor) -> set:
    """读 information_schema；失败返回空集，不中断写入。"""
    try:
        cursor.execute("SAVEPOINT before_cols")
        cursor.execute(
            """
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'persons'
            """
        )
        cols = {row[0] for row in cursor.fetchall()}
        cursor.execute("RELEASE SAVEPOINT before_cols")
        return cols
    except Exception:
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT before_cols")
        except Exception:
            pass
        return set()


def _upsert_person(cursor, data: dict, content_hash: str, counts: dict, cols: set):
    fields = [f for f in _PERSON_CORE_FIELDS if not cols or f in cols]
    params = {}
    for f in fields:
        if f == "current_address":
            params[f] = data.get("current_address_text")
        else:
            params[f] = data.get(f)
    if "content_hash" in cols:
        fields.append("content_hash")
        params["content_hash"] = content_hash
    if all(c in cols for c in _COUNT_FIELDS):
        fields.extend(_COUNT_FIELDS)
        params.update(counts)
    elif cols:
        for name in _COUNT_FIELDS:
            if name in cols:
                fields.append(name)
                params[name] = counts[name]

    col_sql = ", ".join(fields)
    placeholders = ", ".join(f"%({f})s" for f in fields)
    update_parts = [f"{f}=VALUES({f})" for f in fields if f != "person_id"]
    if "scraped_at" in cols:
        update_parts.append("scraped_at=CURRENT_TIMESTAMP")
    cursor.execute(
        f"INSERT INTO persons ({col_sql}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {', '.join(update_parts)}",
        params,
    )


def _upsert_children(cursor, data: dict):
    person_id = data.get("person_id")

    aliases = [
        (person_id, a.get("alias_name"))
        for a in (data.get("aliases") or [])
        if a.get("alias_name")
    ]
    if aliases:
        cursor.executemany(
            """
            INSERT INTO aliases (person_id, alias_name) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE alias_name=VALUES(alias_name)
            """,
            aliases,
        )

    ca = data.get("current_address_detail") if isinstance(data.get("current_address_detail"), dict) else (data.get("current_address") if isinstance(data.get("current_address"), dict) else {})
    if any(
        ca.get(k) is not None
        for k in (
            "street", "unit", "city", "state", "zip_code", "county",
            "estimated_value", "bathrooms", "square_feet", "year_built",
            "hoa_fee_monthly",
        )
    ):
        cursor.executemany(
            """
            INSERT INTO current_addresses
                (person_id, street, unit, city, state, zip_code, county,
                 estimated_value, bathrooms, square_feet, year_built, hoa_fee_monthly)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                street=VALUES(street), unit=VALUES(unit), city=VALUES(city),
                state=VALUES(state), zip_code=VALUES(zip_code), county=VALUES(county),
                estimated_value=VALUES(estimated_value), bathrooms=VALUES(bathrooms),
                square_feet=VALUES(square_feet), year_built=VALUES(year_built),
                hoa_fee_monthly=VALUES(hoa_fee_monthly)
            """,
            [(
                person_id,
                ca.get("street"),
                ca.get("unit"),
                ca.get("city"),
                ca.get("state"),
                ca.get("zip_code"),
                ca.get("county"),
                ca.get("estimated_value"),
                ca.get("bathrooms"),
                ca.get("square_feet"),
                ca.get("year_built"),
                ca.get("hoa_fee_monthly"),
            )],
        )

    prev_addrs = [
        (
            person_id,
            a.get("street"),
            a.get("city"),
            a.get("state"),
            a.get("zip_code"),
            a.get("county"),
        )
        for a in (data.get("previous_addresses") or [])
    ]
    if prev_addrs:
        cursor.executemany(
            """
            INSERT INTO previous_addresses
                (person_id, street, city, state, zip_code, county)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                city=VALUES(city), state=VALUES(state), county=VALUES(county)
            """,
            prev_addrs,
        )

    phones = [
        (
            person_id,
            p.get("phone_number"),
            p.get("line_type"),
            p.get("carrier"),
            p.get("is_primary"),
            p.get("last_reported"),
        )
        for p in (data.get("phone_numbers") or [])
        if p.get("phone_number")
    ]
    if phones:
        cursor.executemany(
            """
            INSERT INTO phone_numbers
                (person_id, phone_number, line_type, carrier, is_primary, last_reported)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                line_type=VALUES(line_type), carrier=VALUES(carrier),
                is_primary=VALUES(is_primary), last_reported=VALUES(last_reported)
            """,
            phones,
        )

    emails = [
        (person_id, e.get("email"))
        for e in (data.get("emails") or [])
        if e.get("email")
    ]
    if emails:
        cursor.executemany(
            """
            INSERT INTO email_addresses (person_id, email) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE email=VALUES(email)
            """,
            emails,
        )

def _try_update_counts(cursor, person_id: str, content_hash: str, counts: dict, cols: set):
    """写回冗余计数 / content_hash；列未 migrate 时不让整单失败。"""
    assignments = []
    params = []
    if not cols or "content_hash" in cols:
        assignments.append("content_hash=%s")
        params.append(content_hash)
    for name in _COUNT_FIELDS:
        if not cols or name in cols:
            assignments.append(f"{name}=%s")
            params.append(counts[name])
    if not assignments:
        return
    params.append(person_id)
    try:
        cursor.execute("SAVEPOINT after_children")
        cursor.execute(
            f"UPDATE persons SET {', '.join(assignments)} WHERE person_id=%s",
            params,
        )
        cursor.execute("RELEASE SAVEPOINT after_children")
    except Exception:
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT after_children")
        except Exception:
            pass


def insert_person(db, data: dict) -> bool:
    """返回 True 仅表示记录已提交或确认已存在；质量跳过返回 False。"""
    if not data.get("person_id") or not data.get("full_name"):
        print("[SKIP_INVALID] 缺少人物标识或姓名，未入库")
        return False
    if REQUIRE_PHONE and not has_usable_phone(data):
        print("[SKIP_NO_PHONE] 未解析到有效电话号码，未入库")
        return False
    if not has_usable_phone(data):
        print("[STORE_NO_PHONE] TPS_REQUIRE_PHONE=0：无有效电话仍入库（仅人名/地址）")

    cursor = db.cursor()
    person_id = data.get("person_id")
    content_hash = compute_content_hash(data)
    counts = _child_counts(data)

    try:
        cols = _persons_columns(cursor)

        if "content_hash" in cols and person_id:
            cursor.execute(
                "SELECT content_hash FROM persons WHERE person_id = %s",
                (person_id,),
            )
            existing = cursor.fetchone()
            if existing and existing[0] == content_hash:
                if "scraped_at" in cols:
                    cursor.execute(
                        "UPDATE persons SET scraped_at = CURRENT_TIMESTAMP "
                        "WHERE person_id = %s",
                        (person_id,),
                    )
                db.commit()
                print(f"[SKIP] {data.get('full_name')} ({person_id}) unchanged")
                return True

        _upsert_person(cursor, data, content_hash, counts, cols)
        _upsert_children(cursor, data)
        _try_update_counts(cursor, person_id, content_hash, counts, cols)

        db.commit()
        print(f"[OK] {data.get('full_name')} ({person_id})")
        print(
            f"   phones={counts['phone_count']} emails={counts['email_count']} "
            f"aliases={counts['alias_count']} prev_addr={counts['prev_addr_count']}"
        )
        return True

    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()


# ============================================================
# 主流程
# ============================================================

def session_kwargs() -> dict:
    """One long-lived stealth browser. Reuse the tab; do not launch per URL."""
    # 自研过 CF 时关闭内置求解避免打架 (TPS_OWN_CF=="1" 时关闭)
    kwargs = {
        "solve_cloudflare": os.environ.get("TPS_OWN_CF") != "1",
        "headless": True,
        "network_idle": False,
        "timeout": FETCH_TIMEOUT_MS,
        "disable_resources": True,
        "block_ads": True,
        "load_dom": True,
        # referer 改为每次请求轮换（见 fetch_kwargs），会话级不再全站 Google。
        "google_search": False,
        "retries": 1,
        "retry_delay": 0,
        "max_pages": 1,
        "selector_config": StealthyFetcher._generate_parser_arguments(),
        # 美区代理 + en-US 头 + 美区时区三者对齐；客户机系统若是中文时区会穿帮。
        "locale": (os.environ.get("TPS_LOCALE") or "en-US").strip() or "en-US",
        "timezone_id": (os.environ.get("TPS_TIMEZONE") or "America/New_York").strip() or "America/New_York",
    }
    if os.path.isfile(STEALTH_INIT_JS):
        kwargs["init_script"] = STEALTH_INIT_JS
    return kwargs


def fetch_kwargs(url: str = "") -> dict:
    # 自研过 CF 时关闭内置求解避免打架 (TPS_OWN_CF=="1" 时关闭)
    kwargs = {
        "solve_cloudflare": os.environ.get("TPS_OWN_CF") != "1",
        "network_idle": False,
        "timeout": FETCH_TIMEOUT_MS,
        "disable_resources": True,
        "load_dom": True,
        # per-request referer 会覆盖会话默认，scrapling 认 extra_headers 里的 referer。
        "google_search": False,
    }
    referer = pick_referer(url)
    if referer:
        kwargs["extra_headers"] = {"referer": referer}
    return kwargs


def open_stealth_session():
    from scrapling.engines._browsers._stealth import StealthySession

    session = StealthySession(**session_kwargs())
    session.start()
    return session


async def open_async_stealth_session(max_pages: int = 1, proxy: str = None):
    """One Chrome, several tabs. Sync sessions ignore max_pages; the async one does not."""
    from scrapling.engines._browsers._stealth import AsyncStealthySession

    kwargs = session_kwargs()
    kwargs["max_pages"] = max(1, min(int(max_pages), 8))
    if proxy:
        kwargs["proxy"] = proxy
        kwargs["block_webrtc"] = True
    session = AsyncStealthySession(**kwargs)
    await session.start()
    return session


_CHALLENGE_MARKERS = (
    "internalcaptcha",
    "just a moment",
    "cf-challenge",
    "cf-turnstile",
    "attention required",
    "checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "请稍候",
    "access denied",
)


class HtmlPage:
    """协议响应：只有 HTML 正文，接口与浏览器页一致，供 parse_person 使用。"""

    def __init__(self, selector, status: int):
        self._selector = selector
        self.status = status

    def get_all_text(self, *args, **kwargs):
        return self._selector.get_all_text(*args, **kwargs)

    def css(self, *args, **kwargs):
        return self._selector.css(*args, **kwargs)


def is_challenge_html(html: str) -> bool:
    text = (html or "").lower()
    if len(text.strip()) < 40:
        return True
    return any(marker in text for marker in _CHALLENGE_MARKERS)


def html_page(html: str, status: int, url: str) -> HtmlPage:
    from scrapling.parser import Selector

    return HtmlPage(Selector(html or "", url=url), int(status))


async def fetch_document(session, url: str):
    """用已经打开的浏览器会话发 HTTP GET，不再为每个人新开渲染页。"""
    context = getattr(session, "context", None)
    request = getattr(context, "request", None)
    if request is None:
        raise RuntimeError("browser context has no request client")
    response = await request.get(
        url,
        timeout=FETCH_TIMEOUT_MS,
        headers={"referer": pick_referer(url) or "https://www.truepeoplesearch.com/"},
    )
    try:
        status = int(response.status)
        body = await response.text()
    finally:
        dispose = getattr(response, "dispose", None)
        if callable(dispose):
            await dispose()
    if status == 429:
        raise HttpError(429, f"HTTP 429 for {url}")
    if status != 200 or is_challenge_html(body):
        return None
    return html_page(body, status, url)


async def fetch_in_async_session(session, url: str):
    try:
        return await session.fetch(url, **fetch_kwargs(url))
    except Exception as exc:
        _raise_fetch_error(exc, url)


def _gateway_page(result):
    if result is None:
        return None
    if result.status in (404, 410):
        return html_page(result.body, result.status, result.url)
    if result.status != 200 or is_challenge_html(result.body):
        return None
    return html_page(result.body, result.status, result.url)


def _raise_gateway(exc, url: str) -> None:
    from cloudbypass_v2 import GatewayError

    if not isinstance(exc, GatewayError):
        raise exc
    if exc.kind == "balance":
        raise HttpError(402, str(exc), bucket="rate_limit") from exc
    if exc.kind == "timeout":
        raise FetchTimeoutError(str(exc)) from exc
    if exc.kind == "rate_limit":
        raise HttpError(429, str(exc), bucket="rate_limit") from exc
    raise ScrapeError(str(exc), bucket="proxy_fail") from exc


async def fetch_cloudbypass_v2(
    url: str,
    part: int = 0,
    timeout: int = 60,
    apikey: Optional[str] = None,
    proxy: Optional[str] = None,
    sitekey: Optional[str] = None,
    max_retries: int = 2,
) -> Optional[HtmlPage]:
    """穿云 v2 Cookie 模式。part 与 sitekey 保留兼容，控制台当前配置不使用它们。"""
    del part, sitekey
    from cloudbypass_v2 import fetch_async

    try:
        result = await fetch_async(
            url,
            apikey=apikey,
            proxy=proxy,
            timeout=timeout,
            max_retries=max_retries,
            session=True,
        )
    except Exception as exc:
        _raise_gateway(exc, url)
        return None
    return _gateway_page(result)


def fetch_cloudbypass_v2_sync(
    url: str,
    part: int = 0,
    timeout: int = 60,
    apikey: Optional[str] = None,
    proxy: Optional[str] = None,
    sitekey: Optional[str] = None,
    max_retries: int = 2,
) -> Optional[HtmlPage]:
    """穿云 v2 Cookie 模式的同步入口。"""
    del part, sitekey
    from cloudbypass_v2 import fetch_sync

    try:
        result = fetch_sync(
            url,
            apikey=apikey,
            proxy=proxy,
            timeout=timeout,
            max_retries=max_retries,
        )
    except Exception as exc:
        _raise_gateway(exc, url)
        return None
    return _gateway_page(result)


def close_session(session) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


def ensure_db(db):
    """Reuse one TiDB connection inside a browser process."""
    if db is not None:
        try:
            if db.is_connected():
                db.ping(reconnect=True, attempts=1, delay=0)
                return db
        except Exception:
            try:
                db.close()
            except Exception:
                pass
    return get_db()


def _raise_fetch_error(exc: BaseException, url: str):
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg or "err_timed_out" in msg or "timed out" in msg:
        raise FetchTimeoutError(f"timeout {FETCH_TIMEOUT_MS}ms for {url}") from exc
    # Chromium 代理层空响应/连接被重置：明确进 retry 桶（可重排到别的 proxy 组），
    # 且文案不带 "empty page" 避免被误判成 empty 成功确认。
    if any(s in msg for s in ("err_empty_response", "err_connection", "err_socket",
                              "err_proxy", "connection reset", "connection closed",
                              "empty response", "broken pipe")):
        raise ScrapeError(f"net_retry {type(exc).__name__}: {exc} for {url}", bucket="retry") from exc
    raise


def _page_document(page) -> str:
    """优先取 HTML；没有则用 parse_person 同一套可见文本。"""
    objects = [page]
    for attr in ("_selector", "response"):
        extra = getattr(page, attr, None)
        if extra is not None and extra not in objects:
            objects.append(extra)
    for obj in objects:
        for name in ("html_content", "html", "body", "content"):
            try:
                val = getattr(obj, name, None)
            except Exception:
                continue
            if isinstance(val, (bytes, bytearray)):
                text = val.decode("utf-8", errors="replace")
            elif isinstance(val, str):
                text = val
            else:
                continue
            if text.strip():
                return text
    get_text = getattr(page, "get_all_text", None)
    if callable(get_text):
        try:
            return str(get_text() or "")
        except Exception:
            return ""
    return ""


def _page_final_url(page, url: str) -> str:
    parts = []
    response = getattr(page, "response", None)
    for obj in (page, response):
        if obj is None:
            continue
        try:
            found = getattr(obj, "url", None)
        except Exception:
            found = None
        if found:
            parts.append(str(found))
    if url:
        parts.append(str(url))
    return " ".join(parts)


def ingest_response(page, url: str, db) -> dict:
    status = getattr(page, "status", None)
    final_url = _page_final_url(page, url)
    document = _page_document(page)
    if is_captcha_document(final_url, document):
        err = HttpError(429, f"HTTP 429 captcha for {url}", bucket="rate_limit")
        err.captcha = True
        raise err
    if status != 200:
        raise HttpError(status, f"HTTP {status} for {url}")

    # 判断是否为电话反查或搜索结果列表页
    is_search = "resultphone" in url.lower() or "/results?" in url.lower() or "resultname" in url.lower() or "/find/phone" in url.lower()
    has_person_path = bool(re.search(r"/(?:find/)?person/([a-zA-Z0-9_]+)", final_url))

    if is_search and not has_person_path:
        person_links = re.findall(r"/find/person/([a-zA-Z0-9_]+)", document)
        queried = _phone_digits_from_url(url)
        if person_links:
            unique_pids = list(dict.fromkeys(person_links))
            try:
                r = _queue_redis()
                from tps_queue import feed
                full_urls = [f"https://www.truepeoplesearch.com/find/person/{pid}" for pid in unique_pids]
                res = feed(r, full_urls, front=True)
                from phone_plan import note_phone_lookup
                note_phone_lookup(r, url, hit=True)
                if queried:
                    try:
                        from phone_plan import remember_associated_phones
                        remember_associated_phones([{"phone_number": _format_us_phone(queried)}])
                    except Exception:
                        pass
                print(f"[SEARCH_RESULT] 电话搜索页面已捕获并注入 {len(unique_pids)} 个目标人物档案 (优先排入队首): {res}", flush=True)
            except Exception as feed_err:
                print(f"[SEARCH_FEED_ERR] 注入队列提示: {feed_err}", flush=True)
            return {"is_search_result": True, "count": len(unique_pids), "person_ids": unique_pids,
                    "queried_phone": _format_us_phone(queried) if queried else ""}
        else:
            try:
                from phone_plan import note_phone_lookup
                r = _queue_redis()
                note_phone_lookup(r, url, hit=False)
            except Exception:
                pass
            raise EmptyPageError(f"电话反查无匹配记录 (0 results): {url}")

    data = parse_person(page, url)
    if not data.get("full_name") or not data.get("person_id"):
        raise EmptyPageError(f"empty page (no valid person): {url}")

    # 电话反查直达人物页：人物 Phone Numbers 分节缺失时，用查询号本身兜底，
    # 保证“查 201xxxxxxx 必有一条号码”可入库、可关联。
    if not data.get("phone_numbers"):
        synth = _synthesize_queried_phone(url)
        if synth:
            data["phone_numbers"] = [synth]
            if not data.get("primary_phone"):
                data["primary_phone"] = synth["phone_number"]

    if not insert_person(db, data):
        raise ScrapeError("parsed person did not meet persistence requirements", bucket="no_phone")
    try:
        from phone_plan import remember_associated_phones

        remembered = remember_associated_phones(data.get("phone_numbers") or [])
        if remembered:
            print(f"[PHONES] 关联号码已一并入库并记入已知集合: {remembered}", flush=True)
    except Exception as phone_err:
        print(f"[PHONES] 关联号码已入库，已知集合更新失败: {phone_err}", flush=True)
    return data


def scrape_with_session(session, url: str, db) -> dict:
    """Fetch with an already-open browser. Caller owns the session lifetime."""
    print(f"\n[FETCH] {url}")
    try:
        page = session.fetch(url, **fetch_kwargs())
    except Exception as exc:
        _raise_fetch_error(exc, url)
    return ingest_response(page, url, db)


def scrape_one(url: str, db) -> dict:
    """One-shot fetch for a single URL. Batch and the worker reuse a session."""
    print(f"\n[FETCH] {url}")

    try:
        page = StealthyFetcher.fetch(
            url,
            solve_cloudflare=True,
            headless=True,
            network_idle=False,
            timeout=FETCH_TIMEOUT_MS,
            disable_resources=True,
            block_ads=True,
        )
    except Exception as exc:
        _raise_fetch_error(exc, url)

    return ingest_response(page, url, db)


def main():
    parser = argparse.ArgumentParser(description="TruePeopleSearch -> TiDB")
    parser.add_argument("--url", help="single person URL")
    parser.add_argument("--batch", help="batch mode, file with URL list")
    args = parser.parse_args()

    db = get_db()

    if args.url:
        scrape_one(args.url, db)

    elif args.batch:
        with open(args.batch) as f:
            urls = [line.strip() for line in f if line.strip()]
        print(f"[BATCH] {len(urls)} URLs")
        success = 0
        failed = 0
        session = None
        try:
            session = open_stealth_session()
            for i, url in enumerate(urls, 1):
                print(f"\n[{i}/{len(urls)}]", end="")
                try:
                    db = ensure_db(db)
                    result = scrape_with_session(session, url, db)
                    if result:
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    print(f"[ERROR] {e}")
                    failed += 1
                    close_session(session)
                    session = None
                    try:
                        session = open_stealth_session()
                    except Exception as open_exc:
                        print(f"[ERROR] reopen browser: {open_exc}")
                        break
        finally:
            close_session(session)
        print(f"\n{'='*60}")
        print(f"[DONE] success={success} failed={failed}")

    else:
        print("Usage: --url <URL> or --batch <file>")

    db.close()


if __name__ == "__main__":
    main()
