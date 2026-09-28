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
from scrapling.fetchers import StealthyFetcher


# ============================================================
# TiDB 连接配置 — 改成你自己的
# ============================================================
TIDB_CONFIG = {
    "host": "127.0.0.1",
    "port": 4000,
    "user": "root",
    "password": "",
    "database": "people_search",
    "autocommit": False,
}


def get_db():
    """获取 TiDB 连接"""
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


FETCH_TIMEOUT_MS = 45_000


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
    无死角提取 Phone Numbers 区域全部电话、类型、主号标记、运营商、最后报告日期
    """
    if not text:
        return []

    # 1. 定位电话区域（兼容单复数 Phone Number / Phone Numbers 及各类后置分节符）
    phone_section = re.search(
        r"Phone Numbers?.*?(?:Email Addresses|Current Address Property Details|Previous Addresses|Possible Relatives|Possible Associates|Businesses|Associated Names|Online Profiles|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    section_text = phone_section.group() if phone_section else text

    phone_regex = re.compile(r"(?:\+?1[-.\s]*)?\(?([2-9]\d{2})\)?[-.\s]*([2-9]\d{2})[-.\s]*(\d{4})")
    matches = list(phone_regex.finditer(section_text))
    if not matches:
        return []

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

        # 1. 线路类型: Wireless / Landline / Landline/Services / VoIP
        type_match = re.search(r"\b(Wireless|Landline(?:/Services)?|VoIP|Voip)\b", block, re.IGNORECASE)
        line_type = "Wireless"
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
        lines = [l.strip() for l in block.splitlines() if l.strip()]
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
            f"{p['phone_number']} ({p.get('line_type') or 'Wireless'})"
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

        chosen = None
        if marked_primary:
            if str(marked_primary.get("line_type", "")).lower() == "wireless":
                chosen = marked_primary
            else:
                # 主要电话是座机或其他：优先选用最近时间的无线号码
                if wireless_sorted:
                    chosen = wireless_sorted[0]
                else:
                    chosen = marked_primary
        else:
            if wireless_sorted:
                chosen = wireless_sorted[0]
            else:
                chosen = parsed_phones[0]

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


def insert_person(db, data: dict):
    """将解析后的人物数据写入 TiDB。失败 rollback 后重抛，禁止吞异常。"""
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
                return

        _upsert_person(cursor, data, content_hash, counts, cols)
        _upsert_children(cursor, data)
        _try_update_counts(cursor, person_id, content_hash, counts, cols)

        db.commit()
        print(f"[OK] {data.get('full_name')} ({person_id})")
        print(
            f"   phones={counts['phone_count']} emails={counts['email_count']} "
            f"aliases={counts['alias_count']} prev_addr={counts['prev_addr_count']}"
        )

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
    return {
        "solve_cloudflare": True,
        "headless": True,
        "network_idle": False,
        "timeout": FETCH_TIMEOUT_MS,
        "disable_resources": True,
        "block_ads": True,
        "load_dom": True,
        "google_search": True,
        "retries": 1,
        "retry_delay": 0,
        "max_pages": 1,
        "selector_config": StealthyFetcher._generate_parser_arguments(),
    }


def fetch_kwargs() -> dict:
    return {
        "solve_cloudflare": True,
        "network_idle": False,
        "timeout": FETCH_TIMEOUT_MS,
        "disable_resources": True,
        "load_dom": True,
        "google_search": True,
    }


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
    "just a moment",
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
        headers={"referer": "https://www.google.com/"},
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
        return await session.fetch(url, **fetch_kwargs())
    except Exception as exc:
        _raise_fetch_error(exc, url)


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
    if "timeout" in name or "timeout" in msg:
        raise FetchTimeoutError(f"timeout {FETCH_TIMEOUT_MS}ms for {url}") from exc
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
    is_search = "resultphone" in url.lower() or "/results?" in url.lower() or "resultname" in url.lower()
    has_person_path = bool(re.search(r"/(?:find/)?person/([a-zA-Z0-9_]+)", final_url))

    if is_search and not has_person_path:
        person_links = re.findall(r"/find/person/([a-zA-Z0-9_]+)", document)
        if person_links:
            unique_pids = list(dict.fromkeys(person_links))
            try:
                import redis
                r = redis.Redis(
                    host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                    port=int(os.environ.get("REDIS_PORT", "6379")),
                    decode_responses=True,
                )
                from tps_queue import feed
                full_urls = [f"https://www.truepeoplesearch.com/find/person/{pid}" for pid in unique_pids]
                res = feed(r, full_urls)
                print(f"[SEARCH_RESULT] 电话搜索页面已捕获并注入 {len(unique_pids)} 个目标人物档案: {res}", flush=True)
            except Exception as feed_err:
                print(f"[SEARCH_FEED_ERR] 注入队列提示: {feed_err}", flush=True)
            return {"is_search_result": True, "count": len(unique_pids), "person_ids": unique_pids}
        else:
            raise EmptyPageError(f"电话反查无匹配记录 (0 results): {url}")

    data = parse_person(page, url)
    if not data.get("full_name") or not data.get("person_id"):
        raise EmptyPageError(f"empty page (no valid person): {url}")

    insert_person(db, data)
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
