#!/usr/bin/env python3
"""
目录发现：字母页 → 姓氏页 → /find/person/{id}，再 feed 进 tps:pending。

用法：
  python3 discover.py --letters a
  python3 discover.py --start https://www.truepeoplesearch.com/find/a --max-dir 20
  python3 discover.py --html-file sample.html --base https://www.truepeoplesearch.com/find/a
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import signal
import sys
import time
from typing import Iterable, List, Optional, Set, Tuple
from urllib.parse import quote, urljoin, urlparse, urlunparse

import redis

from tps_queue import extract_person_id, feed, normalize_url
from tps_metrics import get_metrics
from tps_control import clear_discover_heartbeat, write_discover_heartbeat
from tps_coverage import (
    dir_kind,
    extract_listings,
    load_slice,
    mark_page,
    mark_skipped,
    match_slice,
    normalize_slice,
    persist_slice,
    select_follow_dirs,
)

BASE_HOST = "www.truepeoplesearch.com"
BASE_ORIGIN = "https://www.truepeoplesearch.com"
LETTER_INDEX = "abcdefghijklmnopqrstuvwxyz"

HREF_RE = re.compile(
    r"""(?:href|HREF)\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_SID_RE = re.compile(r"(?i)(-sid-)([A-Za-z0-9]+)")
PERSON_PATH_RE = re.compile(r"^/find/person/(\w+)/?$", re.IGNORECASE)
SKIP_FIRST = frozenset({
    "trending", "top-last-names", "app", "about", "help", "terms",
    "privacy", "contact", "send", "reverse-phone-lookup",
    "address-lookup", "email-lookup",
})

DISCOVER_PENDING = "tps:discover:pending"
DISCOVER_SEEN = "tps:discover:seen"

_STOP = False


def _request_stop(signum, _frame) -> None:
    global _STOP
    _STOP = True
    print(f"[SIGNAL] {signal.Signals(signum).name}, stop after this page")


def connect_redis() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
    )


def parse_letters(spec: str) -> Set[str]:
    spec = (spec or "").strip().lower()
    if not spec or spec in {"all", "*"}:
        return set(LETTER_INDEX)
    out: Set[str] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and len(part) >= 3:
            a, b = part.split("-", 1)
            if len(a) == 1 and len(b) == 1 and a.isalpha() and b.isalpha():
                lo, hi = (a, b) if a <= b else (b, a)
                for i in range(ord(lo), ord(hi) + 1):
                    out.add(chr(i))
                continue
        if len(part) == 1 and part.isalpha():
            out.add(part)
    return out or set(LETTER_INDEX)


def seed_urls(letters: Set[str], start: Optional[str] = None) -> List[str]:
    urls: List[str] = []
    if start:
        urls.append(canonicalize(start) or start)
    for ch in sorted(letters):
        urls.append(f"{BASE_ORIGIN}/find/{ch}")
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def canonicalize(url: str, base: str = BASE_ORIGIN) -> str:
    raw = (url or "").strip()
    if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    abs_url = urljoin(base if base.endswith("/") else base + "/", raw)
    parsed = urlparse(abs_url)
    host = (parsed.netloc or "").lower()
    if host and host != BASE_HOST:
        return ""
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = parsed.query
    if path.startswith("/find/person/"):
        return urljoin(BASE_ORIGIN, path)
    if path.startswith("/results") and query:
        return f"{BASE_ORIGIN}{path}?{query}"
    if path.startswith("/find/"):
        return f"{BASE_ORIGIN}{path}"
    return ""


def classify_path(url: str) -> str:
    """person | directory | other"""
    parsed = urlparse(url)
    path = parsed.path or ""
    if PERSON_PATH_RE.match(path):
        return "person"
    if path.startswith("/find/"):
        first = path[len("/find/"):].split("/", 1)[0].lower()
        if not first or first in SKIP_FIRST:
            return "other"
        return "directory"
    if path.startswith("/results"):
        return "directory"
    return "other"


def allowed_directory(url: str, letters: Set[str]) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    if path.startswith("/results"):
        return True
    if not path.startswith("/find/"):
        return False
    rest = path[len("/find/"):].strip("/")
    if not rest:
        return False
    first = rest.split("/", 1)[0]
    if first in SKIP_FIRST:
        return False
    return first[:1] in letters


def extract_links(html: str, page_url: str) -> Tuple[List[str], List[str]]:
    """返回 (person_urls, directory_urls)，已去重保序。"""
    persons: List[str] = []
    dirs: List[str] = []
    seen_p, seen_d = set(), set()
    for href in HREF_RE.findall(html or ""):
        url = canonicalize(href, page_url)
        if not url:
            continue
        kind = classify_path(url)
        if kind == "person":
            if url not in seen_p:
                seen_p.add(url)
                persons.append(url)
        elif kind == "directory":
            if url not in seen_d:
                seen_d.add(url)
                dirs.append(url)
    return persons, dirs


def is_blocked_html(html: str, url: str = "") -> bool:
    sample = f"{url or ''}\n{(html or '')[:12000]}".lower()
    return any(token in sample for token in (
        "internalcaptcha",
        "just a moment",
        "cf-challenge",
        "attention required",
        "请稍候",
    ))


def page_html(page) -> str:
    for attr in ("html", "body", "content"):
        val = getattr(page, attr, None)
        if isinstance(val, (bytes, bytearray)):
            return val.decode("utf-8", errors="replace")
        if isinstance(val, str) and len(val) > 50:
            return val
    dump = getattr(page, "dump", None)
    if callable(dump):
        try:
            return str(dump())
        except Exception:
            pass
    return str(page)


def browser_proxy(proxy: str) -> str:
    """Chromium 不能给 SOCKS5 带账号密码。浏览器仍走同一网关的 HTTP 代理。"""
    if proxy.startswith("socks5h://"):
        return "http://" + proxy[len("socks5h://"):]
    if proxy.startswith("socks5://"):
        return "http://" + proxy[len("socks5://"):]
    return proxy


def discover_proxy() -> str:
    from proxy_pool import load_proxy_config

    proxy = os.environ.get("PROXY_TUNNEL") or ""
    if not proxy:
        cfg = load_proxy_config()
        if cfg.get("mode") == "tunnel":
            proxy = cfg.get("tunnel") or ""
    if not proxy:
        return ""
    return browser_proxy(proxy)


def _local_refresh_sticky_url(proxy_url: str) -> str:
    """Replace the gateway sid so the next session uses a new exit."""
    raw = (proxy_url or "").strip()
    if not raw:
        return ""
    parts = urlparse(raw if "://" in raw else "http://" + raw)
    user = parts.username or ""
    if not user:
        return raw if "://" in raw else ""
    current = _SID_RE.search(user)
    sid = secrets.token_hex(4)
    while current and sid.lower() == current.group(2).lower():
        sid = secrets.token_hex(4)
    if current:
        username = _SID_RE.sub(lambda m: m.group(1) + sid, user, count=1)
    elif "-region-" in user.lower():
        username = f"{user}-sid-{sid}-t-120"
    else:
        return raw if "://" in raw else ""
    auth = quote(username, safe="")
    password = parts.password or ""
    if password:
        auth += ":" + quote(password, safe="")
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return urlunparse((parts.scheme or "http", f"{auth}@{host}{port}", parts.path or "", "", "", ""))


def _refresh_sticky_proxy(proxy_url: str) -> str:
    try:
        from proxy_pool import refresh_sticky_url
    except ImportError:
        refresh_sticky_url = None
    if callable(refresh_sticky_url):
        refreshed = refresh_sticky_url(proxy_url)
        if isinstance(refreshed, str) and refreshed.strip():
            return refreshed.strip()
    return _local_refresh_sticky_url(proxy_url)


class DirectoryFetcher:
    """一个浏览器一直开着。第一页过验证，后面的姓氏页复用它。"""

    def __init__(self):
        self.session = None
        self.proxy = discover_proxy()

    def fetch(self, url: str):
        from scrapling.engines._browsers._stealth import StealthySession
        from scrape_to_tidb import fetch_kwargs, session_kwargs

        if self.session is None:
            kwargs = session_kwargs()
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self.session = StealthySession(**kwargs)
            self.session.start()
            label = "sticky" if self.proxy else "direct"
            print(f"[BROWSER] discover session proxy={label}", flush=True)
        page = self.session.fetch(url, **fetch_kwargs())
        final_url = ""
        response = getattr(page, "response", None)
        request = getattr(page, "request", None)
        for candidate in (
            getattr(page, "url", None),
            getattr(response, "url", None),
            getattr(request, "url", None),
        ):
            if isinstance(candidate, str) and candidate.strip():
                final_url = candidate.strip()
                break
        if not final_url:
            final_url = url
        return getattr(page, "status", None), page_html(page), final_url

    def close(self) -> None:
        session = self.session
        self.session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def rotate_proxy(self) -> None:
        self.close()
        self.proxy = discover_proxy()


def fetch_html(url: str):
    fetcher = DirectoryFetcher()
    try:
        return fetcher.fetch(url)
    finally:
        fetcher.close()



def is_letter_index(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    return bool(re.fullmatch(r"/find/[a-z]", path))


def push_discover(r, urls: Iterable[str], front: bool = False) -> int:
    added = 0
    pipe = r.pipeline(transaction=False)
    queued = []
    for url in urls:
        pipe.sadd(DISCOVER_SEEN, url)
        queued.append(url)
    flags = pipe.execute() if queued else []
    to_push = [u for u, flag in zip(queued, flags) if int(flag or 0) == 1]
    if to_push:
        if front:
            r.lpush(DISCOVER_PENDING, *reversed(to_push))
        else:
            r.rpush(DISCOVER_PENDING, *to_push)
        added = len(to_push)
    return added


def prioritize_surname_pages(r) -> int:
    """姓氏页排到单个字母页前面，人物链接不用等完 A-Z。"""
    raw = r.lrange(DISCOVER_PENDING, 0, -1) or []
    urls = []
    for item in raw:
        urls.append(item if isinstance(item, str) else item.decode("utf-8", errors="replace"))
    surnames = [u for u in urls if not is_letter_index(u)]
    letters = [u for u in urls if is_letter_index(u)]
    ordered = surnames + letters
    if not surnames or urls == ordered:
        return 0
    pipe = r.pipeline(transaction=False)
    pipe.delete(DISCOVER_PENDING)
    pipe.rpush(DISCOVER_PENDING, *ordered)
    pipe.execute()
    return len(surnames)


def pop_discover(r) -> Optional[str]:
    raw = r.lpop(DISCOVER_PENDING)
    if raw is None:
        return None
    return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")


def process_page(
    r,
    url: str,
    html: str,
    letters: Set[str],
    slice_cfg: dict,
    dry_run: bool = False,
) -> dict:
    """按页类型处理：字母页只扩姓氏；姓氏页先筛卡片再入队，不扩兄弟姓氏。"""
    kind = dir_kind(url)
    persons, dirs = extract_links(html, url)
    listings = extract_listings(html, url)
    if not listings and persons and kind != "letter":
        listings = [{
            "url": u,
            "person_id": extract_person_id(u),
            "name": "",
            "age": None,
            "city": None,
            "state": None,
        } for u in persons]

    follow = select_follow_dirs(url, dirs, letters, slice_cfg)
    result = {
        "kind": kind,
        "listed": 0,
        "in_scope": 0,
        "enqueued": 0,
        "deduped": 0,
        "invalid": 0,
        "skipped": 0,
        "dir_queued": 0,
        "follow": follow,
    }

    if kind == "letter":
        added = len(follow) if dry_run else push_discover(r, follow, front=True)
        result["dir_queued"] = added
        if r is not None and not dry_run:
            mark_page(r, url, kind="letter", listed=0, in_scope=0, fed=0, skipped=0, dirs=added, status="done")
        print(f"[LETTER] follow_surnames={added} queued_front")
        return result

    keep: List[str] = []
    skip_ids: List[str] = []
    for card in listings:
        pid = card.get("person_id") or extract_person_id(card.get("url") or "")
        reason = match_slice(card, slice_cfg)
        if reason is None:
            keep.append(card["url"])
        elif pid:
            skip_ids.append(pid)

    result["listed"] = len(listings)
    result["in_scope"] = len(keep)
    result["skipped"] = len(skip_ids)
    print(
        f"[LIST] kind={kind} listed={len(listings)} in_scope={len(keep)} "
        f"skip={len(skip_ids)} follow={len(follow)}"
    )

    if dry_run:
        result["enqueued"] = len(keep)
        result["dir_queued"] = len(follow)
        for u in keep[:10]:
            print(f"  keep {u}")
        return result

    if skip_ids:
        mark_skipped(r, skip_ids)
    if keep:
        fed = feed(r, keep, seen_check=True)
        result["enqueued"] = int(fed.get("enqueued") or 0)
        result["deduped"] = int(fed.get("deduped") or 0)
        result["invalid"] = int(fed.get("invalid") or 0)
        print(f"[FEED] {fed}")
    added = push_discover(r, follow)
    result["dir_queued"] = added
    if added:
        print(f"[QUEUE] refined+={added}")
    mark_page(
        r, url,
        kind=kind,
        listed=result["listed"],
        in_scope=result["in_scope"],
        fed=result["enqueued"],
        skipped=result["skipped"],
        dirs=added,
        status="done",
    )
    return result


def run_discover(
    letters: Set[str],
    start: Optional[str] = None,
    max_dir: int = 50,
    max_persons: int = 500,
    delay: float = 3.0,
    dry_run: bool = False,
    html_file: Optional[str] = None,
    html_base: Optional[str] = None,
    slice_cfg: Optional[dict] = None,
    r=None,
) -> dict:
    stats = {
        "dir_fetched": 0,
        "persons_found": 0,
        "enqueued": 0,
        "deduped": 0,
        "invalid": 0,
        "dir_queued": 0,
        "skipped": 0,
        "in_scope": 0,
    }
    slice_cfg = slice_cfg or normalize_slice(",".join(sorted(letters)))

    if html_file:
        html = open(html_file, encoding="utf-8", errors="replace").read()
        base = html_base or (start or f"{BASE_ORIGIN}/find/{sorted(letters)[0]}")
        if r is None and not dry_run:
            r = connect_redis()
        page = process_page(r, base, html, letters, slice_cfg, dry_run=dry_run)
        stats["persons_found"] = page["listed"]
        stats["in_scope"] = page["in_scope"]
        stats["enqueued"] = page["enqueued"]
        stats["deduped"] = page["deduped"]
        stats["invalid"] = page["invalid"]
        stats["skipped"] = page["skipped"]
        stats["dir_queued"] = page["dir_queued"]
        stats["dir_fetched"] = 1
        return stats

    if r is None:
        r = connect_redis()
    m = get_metrics(r)
    seeds = seed_urls(letters, start)
    letter_key = "".join(sorted(letters))
    stats["dir_queued"] += push_discover(r, seeds)
    promoted = prioritize_surname_pages(r)
    print(f"[SEED] letters={letter_key} queued={stats['dir_queued']} {seeds[:5]}")
    if promoted:
        print(f"[QUEUE] surname pages moved to front: {promoted}")
    print(
        f"[SLICE] letters={slice_cfg.get('letters')} states={slice_cfg.get('states')} "
        f"cities={slice_cfg.get('cities')} age={slice_cfg.get('age_min')}-{slice_cfg.get('age_max')}"
    )

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    def _hb(url: str = "", status: str = "running") -> None:
        try:
            write_discover_heartbeat(r, {
                "pid": os.getpid(),
                "letters": letter_key,
                "url": url,
                "dir_fetched": stats["dir_fetched"],
                "persons_found": stats["persons_found"],
                "enqueued": stats["enqueued"],
                "skipped": stats["skipped"],
                "dir_queued": stats["dir_queued"],
                "status": status,
            })
        except Exception as exc:
            print(f"[HB] {exc}", file=sys.stderr)

    _hb(status="starting")
    fetcher = DirectoryFetcher()

    while not _STOP:
        if max_dir > 0 and stats["dir_fetched"] >= max_dir:
            break
        if max_persons > 0 and stats["enqueued"] >= max_persons:
            break
        url = pop_discover(r)
        if not url:
            print("[WAIT] discover queue empty")
            break
        if classify_path(url) == "directory" and not allowed_directory(url, letters):
            print(f"[SKIP] {url}")
            continue

        print(f"\n[FETCH] {url}")
        _hb(url)
        try:
            status, html, final_url = fetcher.fetch(url)
        except Exception as exc:
            print(f"[FAIL] fetch {url}: {exc}")
            try:
                r.rpush(DISCOVER_PENDING, url)
            except Exception:
                pass
            fetcher.close()
            time.sleep(delay)
            continue

        stats["dir_fetched"] += 1
        if is_blocked_html(html, final_url):
            r.rpush(DISCOVER_PENDING, url)
            print(f"[BLOCK] {url} captcha page queued to back, pause 2s", flush=True)
            fetcher.rotate_proxy()
            time.sleep(2)
            continue
        if status and int(status) == 429:
            r.lpush(DISCOVER_PENDING, url)
            print("[WARN] HTTP 429, surname page returned to front, pause 300s")
            time.sleep(300)
            continue
        if status and int(status) != 200:
            print(f"[WARN] HTTP {status}")
            time.sleep(delay)
            continue

        page = process_page(r, url, html, letters, slice_cfg, dry_run=dry_run)
        stats["persons_found"] += page["listed"]
        stats["in_scope"] += page["in_scope"]
        stats["enqueued"] += page["enqueued"]
        stats["deduped"] += page["deduped"]
        stats["invalid"] += page["invalid"]
        stats["skipped"] += page["skipped"]
        stats["dir_queued"] += page["dir_queued"]
        if page["deduped"]:
            try:
                m.incr("dedup_hit", page["deduped"])
            except Exception:
                pass

        if not _STOP:
            time.sleep(max(0.0, delay))

    fetcher.close()
    pending_left = int(r.llen(DISCOVER_PENDING) or 0)
    print(
        f"\n[DONE] dir_fetched={stats['dir_fetched']} listed={stats['persons_found']} "
        f"in_scope={stats['in_scope']} enqueued={stats['enqueued']} skipped={stats['skipped']} "
        f"deduped={stats['deduped']} discover_pending={pending_left}"
    )
    try:
        clear_discover_heartbeat(r)
    except Exception:
        pass
    return stats


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="TPS directory discover → person queue")
    parser.add_argument("--letters", default="a", help="a 或 a-c 或 all")
    parser.add_argument("--start", help="起始目录 URL，默认 /find/{letter}")
    parser.add_argument("--max-dir", type=int, default=30, help="最多抓多少个目录页")
    parser.add_argument("--max-persons", type=int, default=500)
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--states", default="", help="州缩写，逗号分隔，空=不限")
    parser.add_argument("--cities", default="", help="城市，逗号分隔，空=不限")
    parser.add_argument("--age-min", default=None)
    parser.add_argument("--age-max", default=None)
    parser.add_argument("--reset-queue", action="store_true", help="重建发现队列（面板启动时已处理）")
    parser.add_argument("--dry-run", action="store_true", help="只解析打印，不入队")
    parser.add_argument("--html-file", help="用本地 HTML 测解析，不访问网站")
    parser.add_argument("--html-base", help="--html-file 时的页面 URL")
    args = parser.parse_args()

    letters = parse_letters(args.letters)
    if args.start:
        path = urlparse(args.start).path.lower()
        m = re.match(r"/find/([a-z])/?$", path)
        if m:
            letters = {m.group(1)}

    r = None if args.dry_run and args.html_file else connect_redis()
    if r is not None:
        persisted = load_slice(r)
        has_cli_filter = bool(args.states or args.cities or args.age_min not in (None, "") or args.age_max not in (None, ""))
        if has_cli_filter:
            slice_cfg = normalize_slice(args.letters, args.states, args.cities, args.age_min, args.age_max)
            persist_slice(r, slice_cfg)
        else:
            slice_cfg = persisted
            slice_cfg["letters"] = args.letters or persisted.get("letters") or "a"
        if args.reset_queue:
            from tps_coverage import rebuild_discover_queue
            rebuild_discover_queue(r, args.letters)
    else:
        slice_cfg = normalize_slice(args.letters, args.states, args.cities, args.age_min, args.age_max)

    run_discover(
        letters=letters,
        start=args.start,
        max_dir=args.max_dir,
        max_persons=args.max_persons,
        delay=args.delay,
        dry_run=args.dry_run,
        html_file=args.html_file,
        html_base=args.html_base,
        slice_cfg=slice_cfg,
        r=r,
    )


if __name__ == "__main__":
    main()
