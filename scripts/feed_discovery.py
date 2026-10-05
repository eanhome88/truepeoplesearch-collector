#!/usr/bin/env python3
"""姓名目录爬 ID 供料管线（只新增文件，不碰现有模块）。

链路：
  sitemap2.xml 下 315 个 names-<字母>-<序号>.xml.gz（约 1500 万条）
    -> 纯本地解析，批量取出姓名目录页 URL（/find/<姓>/<名>），不发请求
    -> 目录页经已求解 lane 抓取：默认复用 http://127.0.0.1:8191/v1
       （byparr 风味 request.get），或 --lane stealth 直连 StealthySession；
       单次尝试，失败只记数不重试（普通出口会被 PerimeterX 按分拦）
    -> 只从卡片 data-detail-link 取 /find/person/<约 19 位 ID>（实测 18–21），
       落 /tmp/feed_ids.txt（仓外）

节流：请求间隔 >= 3 秒，总请求 <= 60 次（硬钳）。
代理：Python 内读 /tmp/us_pool.txt 第 71-73 行（1 起始），经 127.0.0.1:12880
  出口；日志一律打码，只留 host:port。

用法：
  python3 feed_discovery.py --names-file /tmp/names-a-1.xml.gz --max-requests 3
  python3 feed_discovery.py --dir-urls-file /tmp/dirs.txt --max-requests 5
"""

from __future__ import annotations

import argparse
import gzip
import html as _html
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import urlparse

BASE_ORIGIN = "https://www.truepeoplesearch.com"
BASE_HOST = "www.truepeoplesearch.com"

# 已求解 lane：本地求解器（byparr 风味 /v1）。
SOLVER_URL = os.environ.get("FEED_SOLVER_URL", "http://127.0.0.1:8191/v1")
# 本机转发出口：目录页抓取流量经此出，池里三行是该出口后的轮换 US 出口。
FORWARD_EGRESS = "http://127.0.0.1:12880"
POOL_PATH = os.environ.get("FEED_POOL_PATH", "/tmp/us_pool.txt")
POOL_START_LINE = 71  # 1 起始，含首含尾
POOL_END_LINE = 73

OUT_PATH = "/tmp/feed_ids.txt"

MIN_INTERVAL_SEC = 3.0
MAX_REQUESTS = 60
DEFAULT_TIMEOUT_SEC = 60.0
_FLARE_MAX_TIMEOUT_MS = 120_000
_FLARE_MIN_TIMEOUT_MS = 1_000

_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
_CHILD_RE = re.compile(r"names-[a-z]+-\d+\.xml\.gz$", re.IGNORECASE)
# 站内验证码页标记：命中即视为失败记数，不当正常页解析。
_BLOCK_MARKERS = ("internalcaptcha", "captcha challenge", "<title>captcha")


def _looks_blocked(html: str) -> bool:
    low = (html or "").lower()
    return any(m in low for m in _BLOCK_MARKERS)
_DIR_TWO_SEG_RE = re.compile(r"^/find/([^/]+)/([^/]+)/?$", re.IGNORECASE)
_DETAIL_LINK_RE = re.compile(
    r"""data-detail-link\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE
)
# 有效人物 URL 形如 /find/person/<约 19 位 ID>（任务口径 19 位，
# 实测卡片 ID 为 18–21 位不等：取 18–22 交集，短杂串照样不要）。
_PERSON_ID_RE = re.compile(r"/find/person/([A-Za-z0-9]{18,22})(?:[/?#\"']|$)")


class LaneError(RuntimeError):
    """已求解 lane 单次抓取失败（记数，不重试）。"""


def mask_proxy(proxy: str) -> str:
    """打码：去掉 userinfo，只留 scheme://***@host:port。空串记 direct。"""
    raw = (proxy or "").strip()
    if not raw:
        return "direct"
    try:
        parts = urlparse(raw if "://" in raw else "http://" + raw)
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port else ""
        scheme = parts.scheme or "http"
        return f"{scheme}://***@{host}{port}"
    except Exception:
        return "***"


def load_pool_proxies(
    path: str = POOL_PATH, start: int = POOL_START_LINE, end: int = POOL_END_LINE
) -> list:
    """Python 内读代理池第 start-end 行（1 起始，含首含尾），跳过空行。

    池行形如 user:pass@host:port（无 scheme），统一补成 http:// 全 URL。
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    picked = []
    for ln in lines[start - 1 : end]:
        ln = ln.strip()
        if not ln:
            continue
        if "://" not in ln:
            ln = "http://" + ln
        picked.append(ln)
    if not picked:
        raise LaneError(f"proxy pool {path} lines {start}-{end} is empty")
    return picked


def _split_proxy_auth(proxy: str) -> tuple:
    """拆 userinfo：返回 (裸出口, 用户名, 密码)。"""
    text = (proxy or "").strip()
    if not text:
        return "", "", ""
    try:
        parts = urlparse(text)
    except Exception:
        return text, "", ""
    if not parts.hostname or not parts.scheme:
        return text, "", ""
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    from urllib.parse import unquote

    try:
        user = unquote(parts.username or "")
        pwd = unquote(parts.password or "")
    except Exception:
        user, pwd = "", ""
    return f"{parts.scheme}://{host}", user, pwd


def parse_sitemap_index(xml_text: str) -> list:
    """纯解析 sitemap 索引：只收本站 names-<字母>-<序号>.xml.gz（期望 315 个）。"""
    out, seen = [], set()
    for loc in _LOC_RE.findall(xml_text or ""):
        loc = _html.unescape(loc.strip())
        if not loc or not _CHILD_RE.search(loc.split("?")[0]):
            continue
        try:
            if (urlparse(loc).hostname or "").lower() != BASE_HOST:
                continue
        except Exception:
            continue
        if loc not in seen:
            seen.add(loc)
            out.append(loc)
    return out


def parse_names_sitemap(xml_text: str, origin: str = BASE_ORIGIN) -> list:
    """纯解析 names 子 sitemap：只收 /find/<姓>/<名> 两段式目录页。

    排除 /find/person/*（人物页）、单段（字母页）与站外链接。去重保序。
    """
    out, seen = [], set()
    for loc in _LOC_RE.findall(xml_text or ""):
        loc = _html.unescape(loc.strip())
        if not loc:
            continue
        try:
            parts = urlparse(loc)
        except Exception:
            continue
        host = (parts.hostname or "").lower()
        if host and host != BASE_HOST:
            continue
        m = _DIR_TWO_SEG_RE.match(parts.path or "")
        if not m or m.group(1).lower() == "person":
            continue
        url = f"{origin}{parts.path}"
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def read_names_file(path: str) -> str:
    """读 names 子文件（.xml / .xml.gz 皆可），返回解压后文本。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    if path.lower().endswith(".gz") or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw).decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def extract_person_ids_from_directory_html(html: str) -> list:
    """只从卡片 data-detail-link 取 /find/person/<约 19 位 ID>（实测 18–21，
    宽容收 18–22）。去重保序。

    普通 href 里的人物链接不算（有效 ID 只能从目录卡片拿）。
    """
    out, seen = [], set()
    for dbl, sgl in _DETAIL_LINK_RE.findall(html or ""):
        link = _html.unescape(dbl or sgl or "").strip()
        if not link:
            continue
        m = _PERSON_ID_RE.search(link)
        if not m:
            continue
        pid = m.group(1)
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


class SolvedLaneFetcher:
    """已求解 lane：POST 本地 /v1（byparr 风味），代理走 X-Proxy-* 请求头。

    求解 + 抓取同出口；localhost 直连，不吃环境代理。单次尝试，抛 LaneError。
    """

    def __init__(
        self,
        solver_url: str = SOLVER_URL,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ):
        self.solver_url = (solver_url or SOLVER_URL).rstrip("/")
        self.timeout = max(5.0, float(timeout))
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(self, url: str, proxy: str = "") -> str:
        max_ms = max(
            _FLARE_MIN_TIMEOUT_MS, min(int(self.timeout * 1000), _FLARE_MAX_TIMEOUT_MS)
        )
        body = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": max_ms,
            "max_timeout": (max_ms + 999) // 1000,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if (proxy or "").strip():
            bare, user, pwd = _split_proxy_auth(proxy)
            if bare:
                headers["X-Proxy-Server"] = bare
            if user:
                headers["X-Proxy-Username"] = user
            if pwd:
                headers["X-Proxy-Password"] = pwd
        req = urllib.request.Request(
            self.solver_url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with self._opener.open(req, timeout=self.timeout + 10.0) as resp:
                doc = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            raise LaneError(f"solver lane failed for {url}: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("status") != "ok":
            reason = ""
            if isinstance(doc, dict):
                reason = str(doc.get("message") or doc.get("error") or "")[:150]
            raise LaneError(f"solver did not solve {url}: {reason}")
        sol = doc.get("solution") or {}
        html = sol.get("response") or sol.get("html") or ""
        if not isinstance(html, str) or len(html.strip()) < 40:
            raise LaneError(f"solver returned no usable html for {url}")
        if _looks_blocked(html):
            raise LaneError(f"solver returned captcha/block page for {url}")
        return html


class StealthyDirectFetcher:
    """直连 lane：StealthySession（失败即记，不重试）。"""

    def __init__(self, proxy: str = "", timeout_ms: int = 60000):
        self.proxy = proxy
        self.timeout_ms = int(timeout_ms)

    def fetch(self, url: str, proxy: str = "") -> str:
        from scrapling.engines._browsers._stealth import StealthySession

        use_proxy = (proxy or "").strip() or self.proxy
        session = StealthySession(
            headless=True,
            network_idle=False,
            timeout=self.timeout_ms,
            disable_resources=True,
            block_ads=True,
            **({"proxy": use_proxy} if use_proxy else {}),
        )
        try:
            session.start()
            page = session.fetch(url)
            for attr in ("html", "body", "content"):
                val = getattr(page, attr, None)
                if isinstance(val, (bytes, bytearray)):
                    val = val.decode("utf-8", errors="replace")
                if isinstance(val, str) and len(val.strip()) >= 40:
                    return val
            return str(page)
        except Exception as exc:
            raise LaneError(f"stealth fetch failed for {url}: {exc}") from exc
        finally:
            try:
                session.close()
            except Exception:
                pass


def _read_existing_ids(path: str) -> set:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except OSError:
        return set()


def person_page_urls(ids) -> list:
    """人物 ID -> 可入队的人物页 URL。空 ID 丢掉。"""
    out = []
    seen = set()
    for raw in ids or []:
        pid = (raw or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(f"{BASE_ORIGIN}/find/person/{pid}")
    return out


def run_discovery(
    directory_urls: list,
    fetch_fn,
    *,
    out_path: str = OUT_PATH,
    max_requests: int = MAX_REQUESTS,
    interval_sec: float = MIN_INTERVAL_SEC,
    sleep=time.sleep,
    proxies: tuple = (),
    queue_push=None,
) -> dict:
    """跑供料：硬钳 max_requests<=60、间隔 floor 3 秒；单次尝试失败即记。

    fetch_fn(url, proxy) -> html。IDs 去重后追加进 out_path（仓外）。
    queue_push(urls) 可选：把新人物页推进采集队列，返回值记入 stats["queued"]。
    """
    budget = max(0, min(int(max_requests), MAX_REQUESTS))
    gap = max(float(interval_sec), MIN_INTERVAL_SEC)
    seen_in, targets = set(), []
    for u in directory_urls or []:
        u = (u or "").strip()
        if u and u not in seen_in:
            seen_in.add(u)
            targets.append(u)
    targets = targets[:budget]

    stats = {
        "requested": 0,
        "ok": 0,
        "failed": 0,
        "ids_new": 0,
        "ids_total": 0,
        "queued": None,
        "fail_urls": [],
    }
    found, found_seen = [], set()
    for i, url in enumerate(targets):
        if i > 0:
            sleep(gap)
        proxy = proxies[i % len(proxies)] if proxies else ""
        stats["requested"] += 1
        label = mask_proxy(proxy)
        try:
            html = fetch_fn(url, proxy)
        except Exception as exc:
            stats["failed"] += 1
            stats["fail_urls"].append(url)
            print(f"[FAIL] {url} via={label} err={str(exc)[:150]}", flush=True)
            continue
        stats["ok"] += 1
        n = 0
        for pid in extract_person_ids_from_directory_html(html):
            if pid not in found_seen:
                found_seen.add(pid)
                found.append(pid)
                n += 1
        print(f"[OK] {url} via={label} ids={n}", flush=True)

    existing = _read_existing_ids(out_path)
    fresh = [p for p in found if p not in existing]
    if fresh:
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_path, "a", encoding="utf-8") as fh:
            for pid in fresh:
                fh.write(pid + "\n")
    stats["ids_new"] = len(fresh)
    stats["ids_total"] = len(existing | set(found))
    if fresh and callable(queue_push):
        try:
            stats["queued"] = queue_push(person_page_urls(fresh))
        except Exception as exc:
            stats["queued"] = {"error": str(exc)[:200]}
            print(f"[QUEUE] push failed: {exc}", flush=True)
    print(
        f"[DONE] requested={stats['requested']} ok={stats['ok']} "
        f"failed={stats['failed']} ids_new={stats['ids_new']} "
        f"ids_total={stats['ids_total']} out={out_path}",
        flush=True,
    )
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="姓名目录爬 ID 供料（节流硬钳）")
    parser.add_argument("--names-file", action="append", default=[],
                        help="本地 names 子 sitemap（.xml/.xml.gz，可多传）")
    parser.add_argument("--sitemap-index-file",
                        help="本地 sitemap 索引（只打印子文件数，不请求）")
    parser.add_argument("--dir-urls-file",
                        help="每行一个 /find/<姓>/<名> 目录页 URL")
    parser.add_argument("--max-requests", type=int, default=10,
                        help="目录页请求数（硬上限 60）")
    parser.add_argument("--interval", type=float, default=MIN_INTERVAL_SEC,
                        help="请求间隔秒（下限 3）")
    parser.add_argument("--lane", choices=("solver", "stealth"), default="solver")
    parser.add_argument("--pool-lines", default=f"{POOL_START_LINE}-{POOL_END_LINE}",
                        help="代理池行段，如 71-73")
    parser.add_argument("--out", default=OUT_PATH)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    args = parser.parse_args(argv)

    if args.sitemap_index_file:
        text = open(args.sitemap_index_file, encoding="utf-8",
                    errors="replace").read()
        children = parse_sitemap_index(text)
        print(f"[SITEMAP] children={len(children)} (pure parse, no fetch)")
        for loc in children[:10]:
            print(f"  child {loc}")

    dir_urls: list = []
    for path in args.names_file or []:
        try:
            got = parse_names_sitemap(read_names_file(path))
        except OSError as exc:
            print(f"[SKIP] unreadable {path}: {exc}", file=sys.stderr)
            continue
        print(f"[NAMES] {path} dirs={len(got)}")
        dir_urls.extend(got)
    if args.dir_urls_file:
        with open(args.dir_urls_file, encoding="utf-8", errors="replace") as fh:
            dir_urls.extend([ln.strip() for ln in fh if ln.strip()])

    if not dir_urls:
        print("[IDLE] no directory URLs (sitemap stage is pure-parse, "
              "no requests made)")
        return 0

    try:
        lo, _, hi = args.pool_lines.partition("-")
        proxies = load_pool_proxies(POOL_PATH, int(lo), int(hi or lo))
    except Exception as exc:
        print(f"[PROXY] pool unreadable, direct fallback: {exc}",
              file=sys.stderr)
        proxies = []
    print(f"[EGRESS] forward={mask_proxy(FORWARD_EGRESS)} "
          f"pool={[mask_proxy(p) for p in proxies]}")

    if args.lane == "stealth":
        lane = StealthyDirectFetcher(
            proxy=proxies[0] if proxies else "",
            timeout_ms=int(args.timeout * 1000),
        )
    else:
        lane = SolvedLaneFetcher(timeout=args.timeout)
    run_discovery(
        dir_urls,
        lane.fetch,
        out_path=args.out,
        max_requests=args.max_requests,
        interval_sec=args.interval,
        proxies=tuple(proxies),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
