#!/usr/bin/env python3
"""Bounded sample check: report only DB-verified records and stop on rate limits."""

import sys
import os
import time
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scrapling.fetchers import StealthyFetcher
from proxy_pool import load_proxy_config
from scrape_to_tidb import get_db, has_usable_phone, insert_person, is_captcha_document, parse_person
import redis

# 仅用于发现候选人物链接；是否有效、是否新增均以后续数据库核验为准。
SEED_QUERIES = [
    "https://www.truepeoplesearch.com/results?name=John%20Smith&citystatezip=Houston,%20TX",
    "https://www.truepeoplesearch.com/results?name=David%20Miller&citystatezip=Dallas,%20TX",
    "https://www.truepeoplesearch.com/results?name=James%20Johnson&citystatezip=Austin,%20TX",
    "https://www.truepeoplesearch.com/results?name=Robert%20Williams&citystatezip=San%20Antonio,%20TX",
    "https://www.truepeoplesearch.com/results?name=Michael%20Brown&citystatezip=Fort%20Worth,%20TX",
    "https://www.truepeoplesearch.com/results?name=William%20Jones&citystatezip=El%20Paso,%20TX",
    "https://www.truepeoplesearch.com/results?name=Richard%20Davis&citystatezip=Arlington,%20TX",
]

class RateLimitedError(RuntimeError):
    """A target-side refusal must stop the sample run."""


def _target_url(url):
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.lower() == "https"
            and parsed.hostname in {"truepeoplesearch.com", "www.truepeoplesearch.com"}
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _canonical_person_url(href):
    if not isinstance(href, str):
        return None
    try:
        candidate = urljoin("https://www.truepeoplesearch.com/", href.strip())
    except ValueError:
        return None
    if not _target_url(candidate):
        return None
    parsed = urlsplit(candidate)
    if not parsed.path.startswith("/find/person/p") or len(parsed.path) <= len("/find/person/p"):
        return None
    return urlunsplit(("https", "www.truepeoplesearch.com", parsed.path, "", ""))


def _response_status(page):
    status = int(getattr(page, "status", 0) or 0)
    final_url = str(getattr(page, "url", "") or "")
    if status in (403, 429) or "ratelimited" in final_url.lower():
        raise RateLimitedError(f"target refused request (HTTP {status or 'redirect'})")
    if not _target_url(final_url):
        raise RateLimitedError("target redirected outside its expected origin")
    for field in ("html_content", "html", "body", "content"):
        document = getattr(page, field, None)
        if isinstance(document, bytes):
            document = document.decode("utf-8", errors="replace")
        if isinstance(document, str) and is_captcha_document(final_url, document):
            raise RateLimitedError("target challenge page")
    if hasattr(page, "get_all_text"):
        visible_text = page.get_all_text()
        if isinstance(visible_text, str) and is_captcha_document(final_url, visible_text):
            raise RateLimitedError("target challenge page")
    if hasattr(page, "css"):
        title = page.css("title::text").get()
        if isinstance(title, str) and is_captcha_document(final_url, title):
            raise RateLimitedError("target challenge page")
    return status


def _person_exists(conn, person_id):
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM persons WHERE person_id = %s LIMIT 1", (person_id,))
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def _emit(marker, counters):
    print(marker + " " + json.dumps(counters, ensure_ascii=False, sort_keys=True), flush=True)

def harvest_fresh_urls(fetcher, tunnel, target_count=120):
    """Find candidate URLs without rotating proxy identity or retrying refusals."""
    urls = []
    seen = set()
    for q in SEED_QUERIES:
        if len(urls) >= target_count:
            break
        p = fetcher.fetch(q, proxy=tunnel, headless=True, network_idle=True)
        if _response_status(p) != 200:
            continue
        links = p.css("a::attr(href)").getall()
        for link in links:
            full = _canonical_person_url(link)
            if full is None:
                continue
            if full not in seen:
                seen.add(full)
                urls.append(full)
    return urls

def scrape_single_person(url, tunnel, fetcher=None):
    """Return (outcome, person_id); only a committed row is verified."""
    fetcher = fetcher or StealthyFetcher()
    page = fetcher.fetch(url, proxy=tunnel, headless=True, network_idle=True)
    status = _response_status(page)
    if status != 200:
        return f"http_{status}", None
    data = parse_person(page, url)
    person_id = data.get("person_id")
    if not person_id or not data.get("full_name"):
        return "invalid_person", person_id
    if not has_usable_phone(data):
        return "no_phone", person_id

    conn = get_db()
    try:
        existed = _person_exists(conn, person_id)
        committed = insert_person(conn, data)
        if not committed or not _person_exists(conn, person_id):
            return "not_committed", person_id
        return ("existing" if existed else "new"), person_id
    finally:
        conn.close()

def _configured_proxy():
    client = redis.Redis(
        host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
        db=0,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    config = load_proxy_config(client)
    mode = str(config.get("mode") or "direct").lower()
    if mode == "direct":
        return None
    if mode == "tunnel" and config.get("tunnel"):
        return config["tunnel"]
    raise ValueError("sample check requires a saved direct or tunnel proxy configuration")


def _proxy_label(proxy):
    if not proxy:
        return "direct"
    parsed = urlsplit(proxy)
    if not parsed.hostname:
        return "invalid"
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        target = int(args[0]) if args else 100
    except ValueError:
        print("[ERROR] target must be an integer", flush=True)
        return 1
    if not 1 <= target <= 100:
        print("[ERROR] target must be between 1 and 100", flush=True)
        return 1

    counters = {"attempted": 0, "verified_present": 0, "new_rows": 0, "target": target}
    failed_items = 0
    started = time.monotonic()
    try:
        tunnel = _configured_proxy()
        print(f"[BATCH] Starting bounded sample check via {_proxy_label(tunnel)}; credentials are not logged.", flush=True)
        fetcher = StealthyFetcher()
        urls = harvest_fresh_urls(fetcher, tunnel, target_count=target)
        if not urls:
            print("[BATCH] No candidate person URLs were discovered.", flush=True)
            _emit("TPS_BATCH_RESULT", {**counters, "status": "failed"})
            return 1

        for url in urls[:target]:
            counters["attempted"] += 1
            try:
                outcome, _ = scrape_single_person(url, tunnel, fetcher)
            except RateLimitedError:
                raise
            except Exception as exc:
                print(f"[BATCH] item failed: {type(exc).__name__}", flush=True)
                outcome = "error"
            if outcome in ("new", "existing"):
                counters["verified_present"] += 1
            if outcome == "new":
                counters["new_rows"] += 1
            if outcome not in ("new", "existing"):
                failed_items += 1
            print(f"[BATCH] outcome={outcome}", flush=True)
            _emit("TPS_BATCH_PROGRESS", counters)

        print(f"[BATCH] elapsed_sec={time.monotonic() - started:.1f}", flush=True)
        completed = counters["attempted"] == target and counters["verified_present"] > 0 and failed_items == 0
        status = "completed" if completed else "partial"
        _emit("TPS_BATCH_RESULT", {**counters, "status": status})
        return 0 if completed else 3
    except RateLimitedError as exc:
        print(f"[BATCH] {exc}; stopped without retrying.", flush=True)
        _emit("TPS_BATCH_RESULT", {**counters, "status": "rate_limited"})
        return 2
    except Exception as exc:
        print(f"[BATCH] stopped: {type(exc).__name__}", flush=True)
        _emit("TPS_BATCH_RESULT", {**counters, "status": "failed"})
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
