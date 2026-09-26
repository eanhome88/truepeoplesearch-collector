#!/usr/bin/env python3
"""
切片台账：字母 → 姓氏页 → 列表卡片先筛，再决定是否入队。

进度按切片覆盖统计，不按 2.7 亿。seen 只应由 ack 写入（本模块只维护
queued / skipped / 页台账）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urljoin, urlparse

from tps_queue import (
    DLQ_KEY,
    FAILED_KEY,
    JOB_KEY_PREFIX,
    LEASES_KEY,
    PENDING_KEY,
    PROCESSING_KEY,
    QUEUED_KEY,
    SEEN_KEY,
    SKIPPED_KEY,
    _as_int,
    _as_str,
    extract_person_id,
)

BASE_ORIGIN = "https://www.truepeoplesearch.com"
SLICE_KEY = "tps:cover:slice"
PAGE_KEY = "tps:cover:page"
MIGRATED_KEY = "tps:cover:migrated"
DISCOVER_PENDING = "tps:discover:pending"
DISCOVER_SEEN = "tps:discover:seen"
LISTED_PER_SURNAME = 500
SITE_UNIVERSE = 250_000_000

# 美国姓氏首字母大致占比，用来从已观测的 A 推其他字母姓氏规模。
_LETTER_SHARE_RAW = {
    "a": 0.060, "b": 0.092, "c": 0.076, "d": 0.048, "e": 0.028,
    "f": 0.034, "g": 0.051, "h": 0.072, "i": 0.012, "j": 0.031,
    "k": 0.038, "l": 0.047, "m": 0.094, "n": 0.022, "o": 0.018,
    "p": 0.049, "q": 0.004, "r": 0.052, "s": 0.096, "t": 0.043,
    "u": 0.006, "v": 0.018, "w": 0.058, "x": 0.002, "y": 0.011, "z": 0.010,
}
_LETTER_SHARE_SUM = sum(_LETTER_SHARE_RAW.values())
LETTER_SHARE = {k: v / _LETTER_SHARE_SUM for k, v in _LETTER_SHARE_RAW.items()}

# 各州 18+ 人口约数（人），用于把 2.5 亿切到州。
STATE_ADULT = {
    "CA": 30500000, "TX": 22500000, "FL": 18000000, "NY": 15800000,
    "PA": 10400000, "IL": 9800000, "OH": 9200000, "GA": 8400000,
    "NC": 8300000, "MI": 7900000, "NJ": 7300000, "VA": 6800000,
    "WA": 6100000, "AZ": 5700000, "MA": 5600000, "TN": 5500000,
    "IN": 5300000, "MO": 4800000, "MD": 4800000, "WI": 4700000,
    "CO": 4600000, "MN": 4500000, "SC": 4100000, "AL": 3900000,
    "LA": 3500000, "KY": 3500000, "OR": 3400000, "OK": 3100000,
    "CT": 2900000, "UT": 2400000, "IA": 2500000, "NV": 2500000,
    "AR": 2300000, "MS": 2200000, "KS": 2200000, "NM": 1600000,
    "NE": 1500000, "ID": 1500000, "WV": 1400000, "HI": 1100000,
    "NH": 1100000, "ME": 1100000, "RI": 900000, "MT": 900000,
    "DE": 800000, "SD": 700000, "ND": 600000, "AK": 550000,
    "VT": 520000, "WY": 450000, "DC": 550000,
}

AGE_BANDS = (
    (18, 29, 0.20),
    (30, 39, 0.18),
    (40, 49, 0.16),
    (50, 59, 0.16),
    (60, 69, 0.15),
    (70, 120, 0.15),
)

SKIP_FIRST = frozenset({
    "trending", "top-last-names", "app", "about", "help", "terms",
    "privacy", "contact", "send", "reverse-phone-lookup",
    "address-lookup", "email-lookup",
})

LISTING_HREF_RE = re.compile(
    r"""<a\b[^>]*href=["']([^"']*?/find/person/([A-Za-z0-9]+)[^"']*)["'][^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
AGE_RE = re.compile(r"(?:Age|年龄)\s*[:\s]*(\d{1,3})\b", re.IGNORECASE)
LOC_RE = re.compile(
    r"\b([A-Z][A-Za-z.'-]{0,32}(?:\s+[A-Z][A-Za-z.'-]{0,24}){0,2}),\s*([A-Z]{2})\b"
)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)


def _norm_city(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _as_str(value).lower())


def _strip_tags(html: str) -> str:
    text = SCRIPT_RE.sub(" ", html or "")
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_letters(spec: str) -> List[str]:
    raw = _as_str(spec).strip().lower()
    if not raw or raw in {"all", "*"}:
        return list("abcdefghijklmnopqrstuvwxyz")
    out: List[str] = []
    seen = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and len(part) >= 3:
            a, b = part.split("-", 1)
            if len(a) == 1 and len(b) == 1 and a.isalpha() and b.isalpha():
                lo, hi = (a, b) if a <= b else (b, a)
                for i in range(ord(lo), ord(hi) + 1):
                    ch = chr(i)
                    if ch not in seen:
                        seen.add(ch)
                        out.append(ch)
                continue
        if len(part) == 1 and part.isalpha() and part not in seen:
            seen.add(part)
            out.append(part)
    return out or ["a"]


def parse_csv(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in _as_str(value).split(",") if x.strip()]


def parse_age(value: Any) -> Optional[int]:
    if value in (None, "", False):
        return None
    try:
        age = int(float(value))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > 120:
        return None
    return age


def normalize_slice(
    letters: Any = "a",
    states: Any = "",
    cities: Any = "",
    age_min: Any = None,
    age_max: Any = None,
) -> Dict[str, Any]:
    letter_list = parse_letters(letters)
    spec = _as_str(letters).strip().lower() or ",".join(letter_list)
    if spec in {"all", "*"}:
        spec = "all"
    return {
        "letters": spec,
        "states": [s.upper() for s in parse_csv(states)],
        "cities": parse_csv(cities),
        "age_min": parse_age(age_min),
        "age_max": parse_age(age_max),
    }


def persist_slice(r: Any, cfg: Dict[str, Any]) -> None:
    r.set(SLICE_KEY, json.dumps(cfg, ensure_ascii=False))


def load_slice(r: Any) -> Dict[str, Any]:
    raw = r.get(SLICE_KEY)
    if not raw:
        return normalize_slice()
    try:
        data = json.loads(_as_str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return normalize_slice()
    if not isinstance(data, dict):
        return normalize_slice()
    return normalize_slice(
        data.get("letters") or "a",
        data.get("states") or [],
        data.get("cities") or [],
        data.get("age_min"),
        data.get("age_max"),
    )


def dir_kind(url: str) -> str:
    """letter | surname | refined | person | other"""
    parsed = urlparse(_as_str(url))
    path = (parsed.path or "").lower().rstrip("/")
    if "/find/person/" in path:
        return "person"
    if not path.startswith("/find"):
        return "other"
    rest = path[len("/find"):].strip("/")
    if not rest:
        return "other"
    parts = [p for p in rest.split("/") if p]
    first = parts[0]
    if first in SKIP_FIRST:
        return "other"
    if len(parts) == 1 and len(first) == 1 and first.isalpha():
        return "letter"
    if len(parts) == 1:
        return "surname"
    return "refined"


def _abs_url(href: str, base: str = BASE_ORIGIN) -> str:
    raw = _as_str(href).strip()
    if not raw:
        return ""
    return urljoin(base if base.endswith("/") else base + "/", raw)


def extract_listings(html: str, page_url: str = BASE_ORIGIN) -> List[Dict[str, Any]]:
    """从姓氏/城市列表页解析人物卡片（姓名、年龄、城市、州）。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    blob = html or ""
    for match in LISTING_HREF_RE.finditer(blob):
        href, person_id, inner = match.group(1), match.group(2), match.group(3)
        if person_id in seen:
            continue
        seen.add(person_id)
        after = _strip_tags(blob[match.end(): match.end() + 240])
        age = None
        age_m = AGE_RE.search(after)
        if age_m:
            try:
                age_n = int(age_m.group(1))
            except ValueError:
                age_n = 0
            if 18 <= age_n <= 120:
                age = age_n
        locs = list(LOC_RE.finditer(after))
        loc = locs[0] if locs else None
        city = loc.group(1).strip() if loc else None
        state = loc.group(2).upper() if loc else None
        parsed = urlparse(_abs_url(href, page_url))
        query = parse_qs(parsed.query)
        if query.get("city"):
            city = city or query["city"][0]
        if query.get("state"):
            state = state or _as_str(query["state"][0]).upper()
        name = _strip_tags(inner)
        if len(name) > 80:
            name = name[:80].strip()
        out.append({
            "url": f"{BASE_ORIGIN}/find/person/{person_id}",
            "person_id": person_id,
            "name": name,
            "age": age,
            "city": city,
            "state": state,
        })
    return out


def match_slice(card: Dict[str, Any], cfg: Optional[Dict[str, Any]]) -> Optional[str]:
    """命中返回 None；跳过返回原因：state / city / age。缺字段时从宽保留。"""
    cfg = cfg or {}
    states = [s.upper() for s in (cfg.get("states") or [])]
    cities = cfg.get("cities") or []
    age_min = cfg.get("age_min")
    age_max = cfg.get("age_max")

    if states:
        state = _as_str(card.get("state")).upper()
        if state and state not in states:
            return "state"
    if cities:
        city = _norm_city(card.get("city"))
        wanted = {_norm_city(x) for x in cities}
        if city and city not in wanted:
            return "city"
    age = card.get("age")
    if age is not None:
        try:
            age_n = int(age)
        except (TypeError, ValueError):
            age_n = None
        if age_n is not None:
            if age_min is not None and age_n < int(age_min):
                return "age"
            if age_max is not None and age_n > int(age_max):
                return "age"
    return None


def refined_in_slice(url: str, cfg: Optional[Dict[str, Any]]) -> bool:
    """只跟随与切片州/城相符的细化目录，避免姓氏页把上千个兄弟姓氏再入队。"""
    cfg = cfg or {}
    states = [s.upper() for s in (cfg.get("states") or [])]
    cities = cfg.get("cities") or []
    if not states and not cities:
        return False
    path = urlparse(_as_str(url)).path.lower().rstrip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or parts[0] != "find":
        return False
    loc = parts[2]
    city_part, state = loc, ""
    if "-" in loc:
        city_part, state = loc.rsplit("-", 1)
        state = state.upper()
    if states and state and state not in states:
        return False
    if states and not state:
        return False
    if cities:
        wanted = {_norm_city(c) for c in cities}
        city_norm = _norm_city(city_part.replace("-", " "))
        if city_norm not in wanted:
            return False
    return True


def _slug_letter(url: str) -> str:
    path = urlparse(_as_str(url)).path.lower().rstrip("/")
    if not path.startswith("/find/"):
        return ""
    first = path[len("/find/"):].split("/", 1)[0]
    return first[:1]


def select_follow_dirs(
    page_url: str,
    dirs: Sequence[str],
    letters: Iterable[str],
    slice_cfg: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """字母页只跟姓氏；姓氏页不跟兄弟姓氏，仅在切片有州/城时跟匹配的细化目录。"""
    allowed = set(letters)
    kind = dir_kind(page_url)
    out: List[str] = []
    seen = set()
    for raw in dirs:
        url = _as_str(raw)
        if not url or url == page_url or url in seen:
            continue
        if _slug_letter(url) not in allowed:
            continue
        child = dir_kind(url)
        if kind == "letter" and child == "surname":
            seen.add(url)
            out.append(url)
        elif kind in {"surname", "refined"} and child == "refined" and refined_in_slice(url, slice_cfg):
            seen.add(url)
            out.append(url)
    return out


def mark_skipped(r: Any, person_ids: Sequence[str]) -> int:
    ids = [pid for pid in (_as_str(x) for x in person_ids) if pid]
    if not ids:
        return 0
    return int(r.sadd(SKIPPED_KEY, *ids) or 0)


def mark_page(r: Any, url: str, **fields: Any) -> Dict[str, Any]:
    prev: Dict[str, Any] = {}
    raw = r.hget(PAGE_KEY, url) if hasattr(r, "hget") else None
    if raw:
        try:
            loaded = json.loads(_as_str(raw))
            if isinstance(loaded, dict):
                prev = loaded
        except (TypeError, ValueError, json.JSONDecodeError):
            prev = {}
    rec = {
        "url": url,
        "kind": fields.get("kind") or prev.get("kind") or dir_kind(url),
        "listed": _as_int(fields.get("listed", prev.get("listed", 0))),
        "in_scope": _as_int(fields.get("in_scope", prev.get("in_scope", 0))),
        "fed": _as_int(fields.get("fed", prev.get("fed", 0))),
        "skipped": _as_int(fields.get("skipped", prev.get("skipped", 0))),
        "dirs": _as_int(fields.get("dirs", prev.get("dirs", 0))),
        "status": fields.get("status") or prev.get("status") or "done",
        "fetched_at": int(time.time()),
    }
    r.hset(PAGE_KEY, url, json.dumps(rec, ensure_ascii=False))
    return rec


def _decode_members(values: Iterable[Any]) -> List[str]:
    return [_as_str(x) for x in values if _as_str(x)]


def _iter_set(r: Any, key: str) -> List[str]:
    sscan = getattr(r, "sscan", None)
    if callable(sscan):
        cursor = 0
        out: List[str] = []
        while True:
            cursor, batch = sscan(key, cursor, count=400)
            out.extend(_decode_members(batch or []))
            if int(cursor or 0) == 0:
                break
        return out
    smembers = getattr(r, "smembers", None)
    if callable(smembers):
        return _decode_members(smembers(key) or [])
    return []


def rebuild_discover_queue(r: Any, letters_spec: str) -> int:
    """清空发现队列，放入字母页，并把已见过、属于该字母的姓氏页重新排队。"""
    letters = parse_letters(letters_spec)
    r.delete(DISCOVER_PENDING)
    seeds = [f"{BASE_ORIGIN}/find/{ch}" for ch in letters]
    if seeds:
        r.sadd(DISCOVER_SEEN, *seeds)
        r.rpush(DISCOVER_PENDING, *seeds)
    pushed = len(seeds)
    extra: List[str] = []
    for url in _iter_set(r, DISCOVER_SEEN):
        if dir_kind(url) != "surname":
            continue
        if _slug_letter(url) not in letters:
            continue
        extra.append(url)
    if extra:
        r.rpush(DISCOVER_PENDING, *extra)
        pushed += len(extra)
    return pushed


def _chunks(items: Sequence[str], size: int = 256):
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def prune_refined_discover(r: Any) -> int:
    """去掉姓氏页洪泛出来的城市/州细化目录，只留字母页和姓氏页。"""
    refined = [u for u in _iter_set(r, DISCOVER_SEEN) if dir_kind(u) == "refined"]
    removed = 0
    for chunk in _chunks(refined):
        removed += int(r.srem(DISCOVER_SEEN, *chunk) or 0)
    return removed


def drain_person_queue(r: Any) -> int:
    """清空未筛选的人物积压（pending/processing/dlq/queued），不碰已入库 seen。"""
    ids: List[str] = []
    for key in (PENDING_KEY, PROCESSING_KEY, DLQ_KEY):
        try:
            ids.extend(_as_str(x) for x in (r.lrange(key, 0, -1) or []))
        except Exception:
            pass
    try:
        ids.extend(_as_str(x) for x in (r.zrange(LEASES_KEY, 0, -1) or []))
    except Exception:
        pass
    uniq = [i for i in dict.fromkeys(ids) if i]
    r.delete(
        PENDING_KEY, PROCESSING_KEY, DLQ_KEY, LEASES_KEY,
        QUEUED_KEY, SKIPPED_KEY, FAILED_KEY,
    )
    for chunk in _chunks(uniq):
        r.delete(*[f"{JOB_KEY_PREFIX}{jid}" for jid in chunk])
    return len(uniq)


def align_success_metrics(r: Any, success: int) -> None:
    """丢掉旧积压产生的重试/429 计数，成功数与库内档案对齐。"""
    from tps_metrics import BUCKETS, COUNTER_KEY

    for bucket in BUCKETS:
        key = COUNTER_KEY.format(bucket=bucket)
        if bucket == "success":
            r.set(key, int(success))
        else:
            r.delete(key)
    r.delete("tps:metrics:lat:count:queue_wait_ms")
    r.delete("tps:metrics:lat:sum:queue_wait_ms")


def reset_stale_pipeline(r: Any, kept_person_ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """清洪泛数据：细化目录 + 未筛选人物队列；重建字母/姓氏发现队列。"""
    cfg = load_slice(r)
    persist_slice(r, cfg)
    refined_removed = prune_refined_discover(r)
    discover_rebuilt = rebuild_discover_queue(r, cfg.get("letters") or "a")
    jobs_drained = drain_person_queue(r)
    kept = [_as_str(pid) for pid in (kept_person_ids or []) if _as_str(pid)]
    if kept_person_ids is not None:
        r.delete(SEEN_KEY)
        if kept:
            r.sadd(SEEN_KEY, *kept)
        align_success_metrics(r, len(kept))
    return {
        "refined_removed": refined_removed,
        "discover_rebuilt": discover_rebuilt,
        "jobs_drained": jobs_drained,
        "seen": len(kept) if kept_person_ids is not None else None,
        "surnames_indexed": sum(1 for u in _iter_set(r, DISCOVER_SEEN) if dir_kind(u) == "surname"),
        "slice": cfg,
    }


def migrate_seen_to_queued(r: Any) -> int:
    """历史：feed 时就把 pending 记进 seen。迁到 queued，成功入库才算 seen。"""
    if r.get(MIGRATED_KEY):
        return 0
    moved = 0
    for key in (PENDING_KEY, PROCESSING_KEY):
        try:
            ids = r.lrange(key, 0, -1) or []
        except Exception:
            continue
        for raw_id in ids:
            job_id = _as_str(raw_id)
            if not job_id:
                continue
            raw = r.get(f"{JOB_KEY_PREFIX}{job_id}")
            if not raw:
                continue
            try:
                job = json.loads(_as_str(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(job, dict):
                continue
            pid = _as_str(job.get("person_id")) or extract_person_id(_as_str(job.get("url")))
            if not pid:
                continue
            r.sadd(QUEUED_KEY, pid)
            if r.sismember(SEEN_KEY, pid):
                r.srem(SEEN_KEY, pid)
                moved += 1
    r.set(MIGRATED_KEY, "1")
    return moved


def coverage_snapshot(r: Any) -> Dict[str, Any]:
    cfg = load_slice(r)
    pages: Dict[str, Any] = {}
    try:
        pages = r.hgetall(PAGE_KEY) or {}
    except Exception:
        pages = {}

    listed = in_scope = fed = skipped_pages = letters_done = surnames_done = 0
    recent: List[Dict[str, Any]] = []
    for url, raw in pages.items():
        try:
            rec = json.loads(_as_str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        rec.setdefault("url", _as_str(url))
        listed += _as_int(rec.get("listed"))
        in_scope += _as_int(rec.get("in_scope"))
        fed += _as_int(rec.get("fed"))
        skipped_pages += _as_int(rec.get("skipped"))
        kind = rec.get("kind")
        if rec.get("status") == "done" and kind == "letter":
            letters_done += 1
        if rec.get("status") == "done" and kind == "surname":
            surnames_done += 1
        recent.append(rec)
    recent.sort(key=lambda x: _as_int(x.get("fetched_at")), reverse=True)

    surnames_indexed = 0
    letters_indexed = 0
    for url in _iter_set(r, DISCOVER_SEEN):
        kind = dir_kind(url)
        if kind == "surname":
            surnames_indexed += 1
        elif kind == "letter":
            letters_indexed += 1

    has_filter = bool(
        cfg.get("states")
        or cfg.get("cities")
        or cfg.get("age_min") is not None
        or cfg.get("age_max") is not None
    )
    listed_estimate = 0 if has_filter else surnames_indexed * LISTED_PER_SURNAME
    in_scope_display = in_scope if in_scope > 0 else listed_estimate

    return {
        "slice": cfg,
        "pages": len(pages),
        "letters_done": letters_done,
        "letters_indexed": letters_indexed,
        "surnames_done": surnames_done,
        "surnames_indexed": surnames_indexed,
        "listed": listed,
        "listed_estimate": listed_estimate,
        "in_scope": in_scope,
        "in_scope_display": in_scope_display,
        "estimate": in_scope == 0 and listed_estimate > 0,
        "fed": fed,
        "page_skipped": skipped_pages,
        "skipped": _as_int(r.scard(SKIPPED_KEY)),
        "queued": _as_int(r.scard(QUEUED_KEY)),
        "seen": _as_int(r.scard(SEEN_KEY)),
        "failed": _as_int(r.scard(FAILED_KEY)),
        "listed_per_surname": LISTED_PER_SURNAME,
        "recent": recent[:8],
    }


def describe_dir(url: str) -> Dict[str, Any]:
    url = _as_str(url)
    kind = dir_kind(url)
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    slug = parts[-1] if parts else url
    return {
        "url": url,
        "slug": slug,
        "kind": kind if kind in {"letter", "surname", "refined"} else "other",
        "letter": _slug_letter(url),
    }


def _empty_kind_counts() -> Dict[str, int]:
    return {"letter": 0, "surname": 0, "refined": 0, "other": 0, "total": 0}


def summarize_dirs(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    by_kind = _empty_kind_counts()
    by_status = {"pending": 0, "done": 0, "indexed": 0}
    by_letter: Dict[str, int] = {}
    estimate = 0
    for item in items:
        kind = item.get("kind") or "other"
        if kind not in by_kind:
            kind = "other"
        by_kind[kind] += 1
        by_kind["total"] += 1
        status = item.get("status") or "indexed"
        by_status[status] = by_status.get(status, 0) + 1
        letter = _as_str(item.get("letter"))
        if letter:
            by_letter[letter] = by_letter.get(letter, 0) + 1
        estimate += _as_int(item.get("estimate"))
    return {
        "by_kind": by_kind,
        "by_status": by_status,
        "by_letter": dict(sorted(by_letter.items())),
        "estimate": estimate,
        "total": by_kind["total"],
    }


def discover_inventory(r: Any) -> List[Dict[str, Any]]:
    pending = [_as_str(u) for u in (r.lrange(DISCOVER_PENDING, 0, -1) or []) if _as_str(u)]
    pending_set = set(pending)
    seen = set(_iter_set(r, DISCOVER_SEEN))
    pages: Dict[str, Dict[str, Any]] = {}
    try:
        raw_pages = r.hgetall(PAGE_KEY) or {}
    except Exception:
        raw_pages = {}
    for url, raw in raw_pages.items():
        try:
            rec = json.loads(_as_str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict):
            pages[_as_str(url)] = rec

    items: List[Dict[str, Any]] = []
    for url in seen | pending_set:
        info = describe_dir(url)
        rec = pages.get(url) or {}
        if rec.get("status") == "done":
            status = "done"
        elif url in pending_set:
            status = "pending"
        else:
            status = "indexed"
        listed = _as_int(rec.get("listed"))
        estimate = 0
        if info["kind"] == "surname":
            estimate = listed if listed > 0 else LISTED_PER_SURNAME
        item = {
            **info,
            "status": status,
            "listed": listed,
            "in_scope": _as_int(rec.get("in_scope")),
            "fed": _as_int(rec.get("fed")),
            "skipped": _as_int(rec.get("skipped")),
            "estimate": estimate,
        }
        items.append(item)
    return items


def discover_summary(r: Any) -> Dict[str, Any]:
    return summarize_dirs(discover_inventory(r))


def _match_dir(item: Dict[str, Any], kind: str, letter: str, query: str, status: str) -> bool:
    if kind and item.get("kind") != kind:
        return False
    if status and item.get("status") != status:
        return False
    if letter:
        allowed = set(parse_letters(letter))
        if item.get("letter") not in allowed:
            return False
    if query:
        needle = query.lower()
        if needle not in _as_str(item.get("slug")).lower():
            return False
    return True


def list_discover_tasks(
    r: Any,
    kind: str = "",
    letter: str = "",
    q: str = "",
    status: str = "",
    page: int = 1,
    size: int = 30,
) -> Dict[str, Any]:
    items = discover_inventory(r)
    totals = summarize_dirs(items)
    filtered = [i for i in items if _match_dir(i, kind, letter, q, status)]
    filtered.sort(key=lambda i: (
        0 if i.get("status") == "pending" else 1 if i.get("status") == "indexed" else 2,
        0 if i.get("kind") == "letter" else 1,
        _as_str(i.get("slug")),
    ))
    page = max(1, _as_int(page, 1))
    size = min(100, max(1, _as_int(size, 30)))
    start = (page - 1) * size
    return {
        "totals": totals,
        "filtered": summarize_dirs(filtered),
        "items": filtered[start:start + size],
        "page": page,
        "size": size,
        "total": len(filtered),
        "pages": max(1, (len(filtered) + size - 1) // size) if filtered else 1,
        "query": {"kind": kind, "letter": letter, "q": q, "status": status},
    }


def apply_discover_filter(r: Any, kind: str = "", letter: str = "", q: str = "") -> Dict[str, Any]:
    """按类型/字母/姓氏关键词重建待扫队列，已扫过的页不重新入队。"""
    items = discover_inventory(r)
    keep = [
        i["url"] for i in items
        if i.get("status") != "done" and _match_dir(i, kind, letter, q, "")
    ]
    r.delete(DISCOVER_PENDING)
    if keep:
        r.rpush(DISCOVER_PENDING, *keep)
    return {
        "kept": len(keep),
        "filtered": summarize_dirs([
            i for i in items if i.get("status") != "done" and _match_dir(i, kind, letter, q, "")
        ]),
    }


def age_share(age_min: Any, age_max: Any) -> float:
    if age_min is None and age_max is None:
        return 1.0
    lo = parse_age(age_min)
    hi = parse_age(age_max)
    if lo is None:
        lo = 18
    if hi is None:
        hi = 120
    if lo > hi:
        lo, hi = hi, lo
    total = 0.0
    for start, end, share in AGE_BANDS:
        overlap = max(0, min(hi, end) - max(lo, start) + 1)
        width = end - start + 1
        if width > 0:
            total += share * (overlap / width)
    return min(1.0, max(0.0, total))


def indexed_surnames_by_letter(r: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for url in _iter_set(r, DISCOVER_SEEN):
        if dir_kind(url) != "surname":
            continue
        ch = _slug_letter(url)
        if ch:
            counts[ch] = counts.get(ch, 0) + 1
    return counts


def estimate_surnames_for_letters(letters: Sequence[str], indexed: Dict[str, int]) -> Dict[str, int]:
    observed_a = indexed.get("a") or 1002
    share_a = LETTER_SHARE.get("a") or 0.06
    out: Dict[str, int] = {}
    for ch in letters:
        ch = _as_str(ch).lower()
        if ch in indexed:
            out[ch] = indexed[ch]
        else:
            out[ch] = max(1, int(round(observed_a * (LETTER_SHARE.get(ch) or 0.02) / share_a)))
    return out


def scale_snapshot(
    r: Any,
    cfg: Optional[Dict[str, Any]] = None,
    persons: int = 0,
    use_pages: bool = True,
) -> Dict[str, Any]:
    """四层尺度：2.5 亿上限 → 目录可点 → 当前切片 → 已入库。"""
    cfg = cfg or load_slice(r)
    letters = parse_letters(cfg.get("letters") or "a")
    indexed = indexed_surnames_by_letter(r)
    all_letters = list("abcdefghijklmnopqrstuvwxyz")
    surnames_all = estimate_surnames_for_letters(all_letters, indexed)
    surnames_slice = estimate_surnames_for_letters(letters, indexed)
    directory_all = sum(surnames_all.values()) * LISTED_PER_SURNAME
    directory_raw = sum(surnames_slice.values()) * LISTED_PER_SURNAME

    states = [s.upper() for s in (cfg.get("states") or [])]
    geo_pop = sum(STATE_ADULT.get(st, 0) for st in states)
    if states and geo_pop:
        geo_share = min(1.0, geo_pop / float(SITE_UNIVERSE))
        universe_slice = geo_pop
    else:
        geo_share = 1.0
        universe_slice = SITE_UNIVERSE
        geo_pop = SITE_UNIVERSE

    share_age = age_share(cfg.get("age_min"), cfg.get("age_max"))
    universe_slice = int(universe_slice * share_age)
    directory_slice = int(directory_raw * geo_share * share_age)

    pages_in_scope = 0
    if use_pages:
        try:
            snap = coverage_snapshot(r)
            pages_in_scope = _as_int(snap.get("in_scope"))
        except Exception:
            pages_in_scope = 0
    target = pages_in_scope if pages_in_scope > 0 else directory_slice
    persons_n = _as_int(persons)

    def _pct(part: int, whole: int) -> float:
        if whole <= 0:
            return 0.0
        return part / float(whole)

    if states:
        geo_hint = "州 " + "/".join(states) + " · 从 2.5 亿切出的成年人口"
    else:
        geo_hint = "未限州 · 仍是全美 2.5 亿量级"
    if share_age < 1:
        lo = cfg.get("age_min") if cfg.get("age_min") is not None else 18
        hi = cfg.get("age_max") if cfg.get("age_max") is not None else 120
        geo_hint += f" · 年龄 {lo}–{hi}"

    layers = [
        {
            "id": "universe",
            "label": "站点上限",
            "value": SITE_UNIVERSE,
            "parent": SITE_UNIVERSE,
            "hint": "2.5 亿 · 全美成年人口量级，目录点不完",
        },
        {
            "id": "geo",
            "label": "地理/年龄切片",
            "value": universe_slice,
            "parent": SITE_UNIVERSE,
            "hint": geo_hint,
        },
        {
            "id": "directory",
            "label": "目录可点",
            "value": directory_slice,
            "parent": max(universe_slice, 1),
            "hint": "所选字母姓氏 × 每姓第一页约 500 人，再按州/年龄折扣",
        },
        {
            "id": "done",
            "label": "已入库",
            "value": persons_n,
            "parent": max(target, 1),
            "hint": "已写入数据库的档案",
        },
    ]
    for layer in layers:
        layer["pct_parent"] = _pct(_as_int(layer["value"]), _as_int(layer["parent"]))
        layer["pct_universe"] = _pct(_as_int(layer["value"]), SITE_UNIVERSE)

    letter_rows = []
    for ch in all_letters:
        letter_rows.append({
            "letter": ch,
            "indexed": indexed.get(ch, 0),
            "surnames": surnames_all[ch],
            "estimate": surnames_all[ch] * LISTED_PER_SURNAME,
            "selected": ch in letters,
            "known": ch in indexed,
        })

    selected_states = set(states)
    state_rows = [
        {
            "state": st,
            "pop": pop,
            "selected": st in selected_states,
        }
        for st, pop in sorted(STATE_ADULT.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    age_min = cfg.get("age_min")
    age_max = cfg.get("age_max")
    age_rows = [{
        "id": "",
        "label": "不限年龄",
        "min": None,
        "max": None,
        "share": 1.0,
        "selected": age_min is None and age_max is None,
    }]
    for start, end, share in AGE_BANDS:
        hi = 120 if end >= 120 else end
        age_rows.append({
            "id": f"{start}-{hi}",
            "label": f"{start}+" if end >= 120 else f"{start}–{end}",
            "min": start,
            "max": hi,
            "share": share,
            "selected": age_min == start and age_max == hi,
        })

    return {
        "universe": SITE_UNIVERSE,
        "universe_slice": universe_slice,
        "directory_all": directory_all,
        "directory_slice": directory_slice,
        "target": target,
        "persons": persons_n,
        "pct_of_universe": _pct(persons_n, SITE_UNIVERSE),
        "pct_of_slice": _pct(persons_n, target),
        "geo_share": geo_share,
        "age_share": share_age,
        "geo_pop": geo_pop,
        "surnames_indexed": sum(indexed.get(ch, 0) for ch in letters),
        "surnames_estimated": sum(surnames_slice.values()),
        "letters": letters,
        "letter_rows": letter_rows,
        "state_rows": state_rows,
        "age_rows": age_rows,
        "states": states,
        "cities": cfg.get("cities") or [],
        "slice": cfg,
        "layers": layers,
        "listed_per_surname": LISTED_PER_SURNAME,
    }
