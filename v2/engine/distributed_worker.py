#!/usr/bin/env python3
"""
分布式批量抓取 Worker — Redis 租约队列（禁止 LPOP）

每个并发位是一个进程里的常驻浏览器，不是“一条 URL 一个线程 + 一个新 Chrome”。
Playwright 同步 API 不能跨线程共享，冷启动浏览器大约 30 秒，堆线程会把机器打满。

使用：
  python3 distributed_worker.py --mode feed --file urls.txt
  python3 distributed_worker.py --mode worker
  python3 distributed_worker.py --mode worker --concurrency 20
  python3 distributed_worker.py --mode recover
  python3 distributed_worker.py --mode stats

不传 --concurrency 时按本机内存预算拉起浏览器，目标默认一天 300 万条。
一台机器内存不够时，多台机器用同一个 Redis，每台一个 worker。

关停：Ctrl+C 或 SIGTERM。停 claim，短暂等待在飞页面，未完成任务
nack(retry=True) 回队，再关掉浏览器进程。不要 kill -9。

同一出口遇到 HTTP 429 时，任务 release 回 pending（不增加 attempts）。
普通粘性 IP 暂停领取，浏览器保持打开，等这个 IP 恢复后再抓。
用户名含 -region- 的粘性网关不暂停约 70 分钟：换新 sid（新出口）并重启 Chrome。
refresh_sticky_url 原样返回时仍按原来的暂停。
InternalCaptcha / captcha 页同样 release，不按空页 ACK。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import socket
import sys
import threading
import time
import uuid
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from typing import Any, Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from tps_env import load_project_env
load_project_env(_ROOT_DIR, customer_safe=False)

import redis
try:
    import psutil
except ImportError:
    psutil = None

from tps_queue import (
    DLQ_KEY,
    LEASE_SEC,
    MAX_ATTEMPTS,
    PENDING_KEY,
    ack,
    claim,
    drain_legacy,
    extract_person_id,
    feed,
    heartbeat,
    nack,
    release,
    queue_stats,
    recover_expired,
)
from tps_metrics import get_metrics
from tps_control import clear_worker_heartbeat, write_worker_heartbeat
from proxy_pool import (
    ProxyManager,
    StickyLanes,
    load_proxy_config,
    sticky_lanes_from_config,
    tunnel_sticky_lanes,
)

try:
    from proxy_pool import refresh_sticky_url
except ImportError:
    import re
    import secrets
    from urllib.parse import quote, urlunparse

    def refresh_sticky_url(proxy_url: str, minutes: int = 120) -> str:
        """New 8-char sid on a region gateway. Same string if it is not one."""
        raw = (proxy_url or "").strip()
        if not raw:
            return raw
        parts = urlparse(raw if "://" in raw else "http://" + raw)
        user = parts.username or ""
        host = (parts.hostname or "").lower()
        is_res = (
            "-region-" in user.lower()
            or "-res_" in user.lower()
            or "-res-" in user.lower()
            or "res_us" in user.lower()
            or "cloudbypass" in host
            or "gw-res" in host
        )
        if not is_res:
            return raw
        user_clean = re.sub(r"(?i)-sid-[A-Za-z0-9]+-t-\d+", "", user)
        user_clean = re.sub(r"(?i)-session_[A-Za-z0-9]+", "", user_clean)
        user_clean = re.sub(r"(?i)-session-[A-Za-z0-9]+", "", user_clean)
        hold = max(1, min(int(minutes), 120))
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        sid = "".join(secrets.choice(alphabet) for _ in range(8))
        if "-region-" in user.lower():
            username = f"{user_clean}-sid-{sid}-t-{hold}"
        elif "-session" in user.lower():
            username = f"{user_clean}-session_{sid}"
        else:
            username = user
        password = parts.password or ""
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        host_str = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return urlunparse((
            parts.scheme or "http",
            f"{auth}@{host_str}{port}",
            parts.path or "",
            "",
            "",
            "",
        ))


from tps_scale import (
    COLD_PAGE_SEC,
    DAILY_TARGET,
    TABS_PER_CHROME,
    WARM_PAGE_SEC,
    browsers_for,
    chrome_process_count,
    daily_capacity,
    host_browser_budget,
    max_page_sec,
    resolve_browsers,
)
from scrape_to_tidb import (
    ensure_db,
    fetch_cloudbypass_v2,
    fetch_document,
    fetch_in_async_session,
    ingest_response,
    open_async_stealth_session,
)
import scrape_to_tidb as _scrape_mod

# 异常类由 scrape_to_tidb 导出；尚未落地时按消息分桶
HttpError = getattr(_scrape_mod, "HttpError", None)
EmptyPageError = getattr(_scrape_mod, "EmptyPageError", None)
ScrapeError = getattr(_scrape_mod, "ScrapeError", None)

REDIS_HOST = os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None

HB_INTERVAL_SEC = 20
RECOVER_INTERVAL_SEC = 15
IDLE_LOG_SEC = 30
SESSION_RECYCLE_PAGES = 120
SHUTDOWN_WAIT_SEC = 6
# 目标站返回 429 后全局暂停领取：5 / 15 / 45 分钟。切换出口不得缩短暂停。
RATE_LIMIT_PAUSE_STEPS_SEC = (300, 900, 2700)
_SESSION_RETRY_BUCKETS = frozenset({"cf_fail", "retry", "empty"})

_ACK_BUCKETS = frozenset({"success", "empty"})
_KNOWN_BUCKETS = frozenset({
    "success", "empty", "http_4xx", "rate_limit", "cf_fail", "parse_fail",
    "write_fail", "dedup_hit", "retry", "dlq", "no_phone",
})


def connect_redis() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True,
    )


def metrics_client(r) -> Any:
    try:
        return get_metrics(r)
    except TypeError:
        return get_metrics()


def _is_exc_type(cls) -> bool:
    return isinstance(cls, type) and issubclass(cls, BaseException)


def _mysql_error_types() -> tuple:
    types = []
    try:
        import mysql.connector
        types.append(mysql.connector.Error)
    except Exception:
        pass
    return tuple(types)


_MYSQL_ERRORS = _mysql_error_types()
_HTTP_ERRORS = (HttpError,) if _is_exc_type(HttpError) else ()
_EMPTY_ERRORS = (EmptyPageError,) if _is_exc_type(EmptyPageError) else ()
_SCRAPE_ERRORS = (ScrapeError,) if _is_exc_type(ScrapeError) else ()


def job_id(job: dict) -> str:
    return str(job.get("id") or job.get("person_id") or job.get("url") or "")


def job_url(job: dict) -> str:
    return str(job.get("url") or job.get("source_url") or "")


def plan_chrome_groups(page_count: int, lane_count: int, tabs_per_chrome: int = TABS_PER_CHROME) -> list:
    """每个 Chrome 的标签数。有多条粘性 IP 时，一个 Chrome 只占一条。"""
    pages = max(1, int(page_count))
    tabs = max(1, int(tabs_per_chrome))
    lanes = max(0, int(lane_count))
    if lanes <= 1:
        sizes = []
        left = pages
        while left > 0:
            take = min(tabs, left)
            sizes.append(take)
            left -= take
        return sizes
    group_count = min(lanes, pages)
    base, extra = divmod(pages, group_count)
    sizes = []
    for i in range(group_count):
        want = base + (1 if i < extra else 0)
        sizes.append(max(1, min(tabs, want)))
    return sizes


def rate_limit_pause_sec(streak: int) -> int:
    """第 1 次 429 停 5 分钟，第 2 次 15 分钟，之后 45 分钟。若指定无冷却则返回 0。"""
    override = os.environ.get("RATE_LIMIT_PAUSE_SEC")
    if override is not None:
        try:
            return max(0, int(override))
        except ValueError:
            pass
    if os.environ.get("NO_RATE_LIMIT_COOLDOWN") == "1" or os.environ.get("TPS_NO_COOLDOWN") == "1":
        return 0
    steps = RATE_LIMIT_PAUSE_STEPS_SEC
    idx = min(max(int(streak), 1), len(steps)) - 1
    return steps[idx]


def _proxy_username(proxy: str) -> str:
    raw = (proxy or "").strip()
    if not raw:
        return ""
    try:
        return urlparse(raw if "://" in raw else "http://" + raw).username or ""
    except Exception:
        return ""


def is_dynamic_proxy(proxy: str) -> bool:
    """检查是否为动态住宅代理（如穿云、Decodo等轮换IP），或环境变量已声明无冷却。"""
    if os.environ.get("NO_RATE_LIMIT_COOLDOWN") == "1" or os.environ.get("TPS_NO_COOLDOWN") == "1":
        return True
    if os.environ.get("RATE_LIMIT_PAUSE_SEC", "").strip() in ("0", "none", "false"):
        return True
    raw = (proxy or "").lower()
    if not raw:
        return False
    if "cloudbypass" in raw or "gw-res" in raw:
        return True
    user = _proxy_username(proxy).lower()
    if "-res_" in user or "-res-" in user or "res_us" in user or "-region-" in user:
        return True
    return False


def _is_http_429(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    try:
        if status is not None and int(status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    msg = str(exc).lower()
    return any(token in msg for token in ("http 429", "status 429", " 429"))


def _has_captcha(*parts: object) -> bool:
    """error、url、message 里出现 internalcaptcha / captcha（大小写不敏感）。"""
    for part in parts:
        if isinstance(part, BaseException):
            text = str(part)
        elif isinstance(part, (bytes, bytearray)):
            text = part.decode("utf-8", errors="replace")
        elif isinstance(part, str):
            text = part
        else:
            continue
        folded = text.lower()
        if "internalcaptcha" in folded or "captcha" in folded:
            return True
    return False


def _redact_url(loc: str) -> str:
    """日志只用站点 URL，去掉 userinfo，避免带出代理密码。"""
    if not loc:
        return ""
    try:
        parts = urlsplit(loc)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            loc = urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
    except Exception:
        return ""
    return loc


def _captcha_note(error, loc: str) -> str:
    safe = _redact_url(loc) if isinstance(loc, str) and _has_captcha(loc) else ""
    note = f"captcha {safe}".strip() if safe else "captcha"
    if error is None or error == "":
        return note[:300]
    text = error if isinstance(error, str) else str(error)
    if _has_captcha(text):
        return text[:300]
    return f"{text}; {note}"[:300]


def _page_locations(page, url: str = "") -> list:
    found = []
    if isinstance(url, str) and url:
        found.append(url)
    if page is None:
        return found
    objects = [page]
    for attr in ("response", "_selector"):
        extra = getattr(page, attr, None)
        if extra is not None:
            objects.append(extra)
    for obj in objects:
        try:
            loc = getattr(obj, "url", None)
        except Exception:
            loc = None
        if isinstance(loc, str) and loc and loc not in found:
            found.append(loc)
    history = getattr(page, "history", None)
    if not history:
        return found
    try:
        items = list(history)
    except TypeError:
        return found
    for item in items:
        if isinstance(item, str):
            loc = item
        else:
            try:
                loc = getattr(item, "url", None)
            except Exception:
                loc = None
        if isinstance(loc, str) and loc and loc not in found:
            found.append(loc)
    return found


def _page_title(page) -> str:
    if page is None:
        return ""
    css = getattr(page, "css", None)
    if not callable(css):
        return ""
    try:
        found = css("title::text")
    except Exception:
        return ""
    get = getattr(found, "get", None)
    if not callable(get):
        return found if isinstance(found, str) else ""
    try:
        title = get()
    except Exception:
        return ""
    return str(title or "")


def _page_visible(page) -> str:
    if page is None:
        return ""
    text_fn = getattr(page, "get_all_text", None)
    if not callable(text_fn):
        return ""
    try:
        text = text_fn()
    except Exception:
        return ""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    return str(text or "")[:8000]


def _html_has_internal_captcha(page) -> bool:
    if page is None:
        return False
    objects = [page]
    for attr in ("_selector", "response"):
        extra = getattr(page, attr, None)
        if extra is not None:
            objects.append(extra)
    for obj in objects:
        for name in ("html_content", "html", "body", "content"):
            try:
                val = getattr(obj, name, None)
            except Exception:
                continue
            if isinstance(val, (bytes, bytearray)):
                val = val.decode("utf-8", errors="replace")
            elif not isinstance(val, str):
                continue
            if "internalcaptcha" in val.lower():
                return True
    return False


def _looks_like_captcha(page, url: str = "", include_visible: bool = False) -> bool:
    if _has_captcha(*_page_locations(page, url), _page_title(page)):
        return True
    if _html_has_internal_captcha(page):
        return True
    return bool(include_visible and _has_captcha(_page_visible(page)))


def _captcha_final_url(page, url: str = "") -> str:
    for loc in _page_locations(page, url):
        if _has_captcha(loc):
            return _redact_url(loc)
    locs = _page_locations(page, url)
    if locs:
        return _redact_url(locs[0])
    return _redact_url(url) if isinstance(url, str) else ""


def classify_error(exc: BaseException, url: str = "", message: str = "", page=None) -> str:
    """把 scrape/写库异常分到 metrics bucket。429 先于通用 4xx。验证码页按限流。"""
    if _has_captcha(exc, getattr(exc, "url", None), url, message) or _looks_like_captcha(page, url):
        return "rate_limit"
    bucket = _classify_error_kind(exc)
    if bucket == "empty" and _looks_like_captcha(page, url, include_visible=True):
        return "rate_limit"
    return bucket


def _classify_error_kind(exc: BaseException) -> str:
    """把 scrape/写库异常分到 metrics bucket。429 先于通用 4xx。"""
    if _is_http_429(exc):
        return "rate_limit"
    bucket = getattr(exc, "bucket", None)
    if isinstance(bucket, str) and bucket in _KNOWN_BUCKETS:
        return bucket
    if bucket == "http_5xx":
        return "retry"

    if _HTTP_ERRORS and isinstance(exc, _HTTP_ERRORS):
        status = getattr(exc, "status", None)
        try:
            if status is not None and int(status) >= 500:
                return "retry"
        except (TypeError, ValueError):
            pass
        return "http_4xx"
    if _EMPTY_ERRORS and isinstance(exc, _EMPTY_ERRORS):
        return "empty"
    if _MYSQL_ERRORS and isinstance(exc, _MYSQL_ERRORS):
        return "write_fail"

    status = getattr(exc, "status", None)
    if status is not None:
        try:
            code = int(status)
            if 400 <= code < 500:
                return "http_4xx"
            if code >= 500:
                return "retry"
        except (TypeError, ValueError):
            pass

    msg = str(exc).lower()
    name = type(exc).__name__.lower()

    if any(s in msg for s in ("timeout", "timed out", "timeouterror")):
        return "cf_fail"
    if any(s in msg for s in ("cloudflare", "cf_clearance", "challenge", "cf ray")):
        return "cf_fail"
    if any(s in msg for s in ("http 4", "status 4", " 404", " 403", " 410", "4xx")):
        return "http_4xx"
    if any(s in msg for s in ("http 5", "status 5", " 502", " 503", " 504", "5xx")):
        return "retry"
    if any(s in msg for s in ("mysql", "tidb", "database", "deadlock", "lock wait", "insert")):
        return "write_fail"
    if any(s in name for s in ("operationalerror", "integrityerror", "interfaceerror", "databaseerror")):
        return "write_fail"
    if "empty" in msg and "page" in msg:
        return "empty"
    if _SCRAPE_ERRORS and isinstance(exc, _SCRAPE_ERRORS):
        return "parse_fail"
    if any(s in msg for s in ("parse", "css", "xpath")):
        return "parse_fail"
    return "retry"


def _incr(m, bucket: str) -> None:
    try:
        m.incr(bucket)
    except Exception as exc:
        print(f"[METRICS] incr {bucket}: {exc}", file=sys.stderr)


def _observe(m, name: str, ms: float) -> None:
    try:
        m.observe_ms(name, ms)
    except Exception as exc:
        print(f"[METRICS] observe {name}: {exc}", file=sys.stderr)


def _observe_queue_wait(m, job: dict) -> None:
    for key in ("enqueued_at", "queued_at", "created_at", "fed_at"):
        ts = job.get(key)
        if ts is None:
            continue
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if ts > 1e12:
            wait_ms = max(0.0, time.time() * 1000.0 - ts)
        else:
            wait_ms = max(0.0, (time.time() - ts) * 1000.0)
        _observe(m, "queue_wait_ms", wait_ms)
        return


def _short_err(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:300]


class _QueueTimeout:
    pass


_QUEUE_TIMEOUT = _QueueTimeout()


def _queue_get(q):
    try:
        return q.get(timeout=0.5)
    except Empty:
        return _QUEUE_TIMEOUT


def browser_group_main(slot_ids: list, queues: list, out_q, proxy=None, generation: int = 0) -> None:
    """One Chrome process, several tabs. Playwright stays inside this process."""
    try:
        if hasattr(os, "setsid"):
            os.setsid()
    except OSError:
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        asyncio.run(_browser_group(slot_ids, queues, out_q, proxy, generation))
    except Exception as exc:
        for slot in slot_ids:
            try:
                out_q.put({"slot": slot, "kind": "fatal", "error": _short_err(exc)})
            except Exception:
                pass


async def _browser_group(slot_ids: list, queues: list, out_q, proxy=None, generation: int = 0) -> None:
    box = _ChromeBox(len(slot_ids), proxy=proxy, generation=generation)
    try:
        await asyncio.gather(*[
            _tab_loop(slot, queue, box, out_q)
            for slot, queue in zip(slot_ids, queues)
        ])
    finally:
        await box.close()


class _ChromeBox:
    def __init__(self, tabs: int, proxy=None, generation: int = 0):
        self.tabs = max(1, int(tabs))
        self.proxy = proxy or None
        self.generation = int(generation)
        self.session = None
        self.db = None
        self.warm = False
        self.served = 0
        self.gate = asyncio.Lock()
        self.db_lock = asyncio.Lock()

    async def close(self) -> None:
        session = self.session
        self.session = None
        self.warm = False
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                pass
            self.db = None

    async def ensure_session(self):
        if self.session is not None and self.served >= SESSION_RECYCLE_PAGES:
            await self.close()
            self.served = 0
        if self.session is None:
            self.session = await open_async_stealth_session(self.tabs, proxy=self.proxy)
            label = ProxyManager._mask_proxy(self.proxy) if self.proxy else "direct"
            print(f"[BROWSER] chrome open tabs={self.tabs} proxy={label}", flush=True)
        return self.session


async def _tab_loop(slot: int, queue, box: _ChromeBox, out_q) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, out_q.put, {"slot": slot, "kind": "ready"})
    while True:
        job = await loop.run_in_executor(None, _queue_get, queue)
        if job is _QUEUE_TIMEOUT:
            continue
        if job is None:
            return
        result = await _tab_job(slot, box, job)
        await loop.run_in_executor(None, out_q.put, result)
        await loop.run_in_executor(
            None, out_q.put, {"slot": slot, "kind": "ready", "generation": box.generation},
        )


async def _tab_job(slot: int, box: _ChromeBox, job: dict) -> dict:
    url = job_url(job)
    jid = job_id(job)
    person = job.get("person_id") or (extract_person_id(url) if url else "")
    t0 = time.monotonic()

    def done(bucket: str, error, served_ok: bool, page=None) -> dict:
        final_url = _captcha_final_url(page, url)
        scan_visible = bucket == "empty" or _has_captcha(error)
        if _has_captcha(error, url, final_url) or _looks_like_captcha(page, url, include_visible=scan_visible):
            if not _has_captcha(error):
                error = _captcha_note(error, final_url or url)
            bucket = "rate_limit"
            served_ok = False
        if served_ok:
            box.served += 1
        return {
            "slot": slot,
            "kind": "done",
            "id": jid,
            "bucket": bucket,
            "error": error,
            "scrape_ms": (time.monotonic() - t0) * 1000.0,
            "person": person,
            "url": url,
            "final_url": final_url,
            "generation": box.generation,
        }

    if not url:
        return done("parse_fail", "missing url", False)

    last_bucket = "retry"
    last_error = "fetch failed"
    page = None
    for attempt in (1, 2):
        fetched = None
        try:
            fetched = await _fetch_page(box, url)
            page = fetched
            if _looks_like_captcha(page, url):
                return done("rate_limit", None, False, page)
            async with box.db_lock:
                box.db = await asyncio.to_thread(ensure_db, box.db)
                data = await asyncio.to_thread(ingest_response, page, url, box.db)
            box.warm = True
            bucket = "success" if data else "empty"
            return done(bucket, None, True, page)
        except Exception as exc:
            page = fetched
            last_error = _short_err(exc)
            last_bucket = classify_error(exc, url=url, page=page)
            empty_page = "empty" in last_error.lower() and "page" in last_error.lower()
            captcha = _has_captcha(exc, last_error, url) or _looks_like_captcha(
                page, url, include_visible=(last_bucket == "empty" or empty_page),
            )
            if captcha:
                last_bucket = "rate_limit"
                if not _has_captcha(last_error):
                    last_error = _captcha_note(last_error, _captcha_final_url(page, url))
            if last_bucket == "write_fail":
                async with box.db_lock:
                    if box.db is not None:
                        try:
                            box.db.close()
                        except Exception:
                            pass
                        box.db = None
                break
            if last_bucket in ("http_4xx", "rate_limit"):
                break
            if last_bucket not in _SESSION_RETRY_BUCKETS or attempt == 2:
                break
            print(f"[BROWSER] slot={slot} retry after {last_bucket}", flush=True)
    return done(last_bucket, last_error, False, page)


async def _fetch_page(box: _ChromeBox, url: str):
    """优先走穿云 V2 API 网关抓取；失败或不可用时退回本地协议与浏览器渲染。"""
    if os.environ.get("USE_CLOUDBYPASS", "1") == "1":
        try:
            page = await fetch_cloudbypass_v2(url)
            if page is not None and getattr(page, "status", None) == 200:
                box.warm = True
                return page
        except HttpError:
            raise
        except Exception as e:
            print(f"[CLOUDBYPASS] fallback: {e}", flush=True)

    if box.warm and box.session is not None:
        try:
            page = await fetch_document(box.session, url)
        except Exception:
            print("[PROTO] protocol fetch failed; using rendered page", flush=True)
            page = None
        if page is not None:
            return page
        print("[PROTO] rendering page", flush=True)
        return await fetch_in_async_session(box.session, url)
    async with box.gate:
        session = await box.ensure_session()
        if box.warm and box.session is not None:
            session = box.session
        else:
            page = await fetch_in_async_session(session, url)
            if getattr(page, "status", None) == 200:
                box.warm = True
            return page
    return await fetch_in_async_session(session, url)


class _BrowserSlot:
    def __init__(self, slot: int, ctx):
        self.slot = slot
        self.ctx = ctx
        self.in_q = None
        self.proc = None
        self.job: Optional[dict] = None
        self.jid = ""


class _ChromeGroup:
    def __init__(self, gid: int, slots: list, ctx):
        self.gid = gid
        self.slots = slots
        self.ctx = ctx
        self.proc = None
        self.restarts = 0
        self.proxy = None
        self.generation = 0
        self.swapping = False
        self.claim_after = 0.0
        self.consecutive_rate_limits = 0

    def start(self, out_q) -> None:
        for slot in self.slots:
            slot.in_q = self.ctx.Queue(maxsize=1)
        self.proc = self.ctx.Process(
            target=browser_group_main,
            args=(
                [slot.slot for slot in self.slots],
                [slot.in_q for slot in self.slots],
                out_q,
                self.proxy,
                self.generation,
            ),
            name=f"tps-chrome-{self.gid}",
            daemon=False,
        )
        self.proc.start()
        for slot in self.slots:
            slot.proc = self.proc


class LeaseWorker:
    def __init__(self, r, concurrency: int, target_per_day: int, page_sec: float, lanes=None):
        self.r = r
        self.lanes = lanes
        self.concurrency = max(1, int(concurrency))
        self.target_per_day = int(target_per_day)
        self.page_sec = float(page_sec)
        self.capacity = daily_capacity(self.concurrency, self.page_sec)
        self.need = browsers_for(self.target_per_day, self.page_sec)
        self.budget = host_browser_budget()
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.m = metrics_client(r)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.in_flight: dict[str, dict] = {}
        self.abandoned: set[str] = set()
        self.finalized: set[str] = set()
        self.ctx = get_context("spawn")
        self.out_q = self.ctx.Queue()
        lane_count = self.lanes.count if self.lanes is not None else 0
        sizes = plan_chrome_groups(self.concurrency, lane_count)
        used = sum(sizes)
        if used < self.concurrency:
            print(
                f"[PROXY] {lane_count} sticky IPs cover {used} pages; "
                f"not opening the other {self.concurrency - used}",
                flush=True,
            )
            self.concurrency = used
            self.capacity = daily_capacity(self.concurrency, self.page_sec)
        self.slots = [_BrowserSlot(i, self.ctx) for i in range(self.concurrency)]
        groups = []
        start = 0
        for gid, size in enumerate(sizes):
            group = _ChromeGroup(gid, self.slots[start:start + size], self.ctx)
            if self.lanes is not None:
                group.proxy = self.lanes.checkout(str(gid))
            groups.append(group)
            start += size
        self.groups = groups
        self.idle: set[int] = set()
        self._last_idle = 0.0
        self._claim_after = 0.0
        self._rate_limit_streak = 0

    def _on_signal(self, signum, _frame) -> None:
        name = signal.Signals(signum).name
        if self.stop.is_set():
            print(f"[SIGNAL] {name} again, force exit")
            self._stop_browsers()
            os._exit(1)
        print(f"[SIGNAL] {name}, stop claim; waiting in-flight...")
        self.stop.set()

    def _proc_heartbeat(self) -> None:
        with self.lock:
            inflight = [
                {
                    "id": jid,
                    "person_id": job.get("person_id"),
                    "url": job_url(job),
                }
                for jid, job in self.in_flight.items()
            ]
        try:
            write_worker_heartbeat(self.r, {
                "worker_id": self.worker_id,
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "concurrency": self.concurrency,
                "inflight": inflight,
                "status": self._heartbeat_status(),
                "pause_remaining_sec": self._pause_remaining_sec(),
                "capacity_per_day": self.capacity,
                "target_per_day": self.target_per_day,
                "page_sec": self.page_sec,
                "browsers_need": self.need,
                "host_budget": self.budget,
            })
        except Exception as exc:
            print(f"[HB] process: {exc}", file=sys.stderr)

    def _print_scale(self) -> None:
        chromes = chrome_process_count(self.concurrency, TABS_PER_CHROME)
        print(
            f"[SCALE] pages={self.concurrency} chrome={chromes} "
            f"tabs_per_chrome={TABS_PER_CHROME} warm_page={self.page_sec:.1f}s "
            f"capacity≈{self.capacity:,}/day target={self.target_per_day:,}/day "
            f"need={self.need} host_budget={self.budget} "
            f"cold_threads_would_need={browsers_for(self.target_per_day, COLD_PAGE_SEC)}"
        )
        need_sec = max_page_sec(self.concurrency, self.target_per_day)
        budget_hosts = math.ceil(self.need / self.budget) if self.budget else self.need
        if self.concurrency > self.budget:
            print(
                f"[SCALE] {self.concurrency} browsers is above this host's budget "
                f"({self.budget}). Page time will rise once the CPUs are saturated."
            )
        if self.capacity >= self.target_per_day:
            print("[SCALE] this process can hold the daily target in steady state")
        else:
            print(
                f"[SCALE] at {self.page_sec:.1f}s/page this process does about "
                f"{self.capacity:,}/day. Hitting {self.target_per_day:,}/day needs "
                f"warm pages under {need_sec:.2f}s, or {budget_hosts} hosts at "
                f"--concurrency {self.budget} on the same Redis."
            )

    def run(self) -> None:
        print(
            f"[WORKER] id={self.worker_id} pages={self.concurrency} "
            f"chrome={chrome_process_count(self.concurrency, TABS_PER_CHROME)} "
            f"redis={REDIS_HOST}:{REDIS_PORT} lease={LEASE_SEC}s "
            f"max_attempts={MAX_ATTEMPTS}"
        )
        self._print_scale()
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        self._proc_heartbeat()
        threading.Thread(
            target=self._heartbeat_loop, name="tps-heartbeat", daemon=True,
        ).start()
        threading.Thread(
            target=self._recover_loop, name="tps-recover", daemon=True,
        ).start()

        try:
            n = recover_expired(self.r)
            if n:
                print(f"[RECOVER] startup reclaimed={n}")
        except Exception as exc:
            print(f"[RECOVER] startup: {exc}", file=sys.stderr)

        for group in self.groups:
            group.start(self.out_q)
        try:
            self._dispatch_loop()
        finally:
            self._shutdown()

    def _dispatch_loop(self) -> None:
        while not self.stop.is_set():
            self._drain_results(block=0)
            self._reap_slots()
            idle_before = len(self.idle)
            if self.idle and not self.stop.is_set():
                self._claim_one()
            if len(self.idle) == idle_before:
                self._drain_results(block=0.4)

    def _drain_results(self, block: float) -> None:
        deadline = time.monotonic() + block
        while True:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                msg = self.out_q.get(timeout=timeout)
            except Empty:
                break
            self._handle_msg(msg)

    def _handle_msg(self, msg: dict) -> None:
        slot_id = int(msg.get("slot") or 0)
        if slot_id < 0 or slot_id >= len(self.slots):
            return
        slot = self.slots[slot_id]
        kind = msg.get("kind")
        if kind == "ready":
            group = self._group_of(slot.slot)
            generation = msg.get("generation")
            if generation is not None and int(generation) != group.generation:
                return
            group.swapping = False
            if slot.job is None and not self._group_resting(group):
                self.idle.add(slot.slot)
        elif kind == "done":
            self._on_done(slot, msg)
        elif kind == "fatal":
            print(f"[BROWSER] slot={slot.slot} fatal", file=sys.stderr)

    def _on_done(self, slot: _BrowserSlot, msg: dict) -> None:
        jid = str(msg.get("id") or slot.jid or "")
        with self.lock:
            job = self.in_flight.pop(jid, None) or slot.job
        slot.job = None
        slot.jid = ""
        if job is None:
            return
        slot.restarts = 0
        bucket = str(msg.get("bucket") or "retry")
        error = msg.get("error")
        try:
            scrape_ms = float(msg.get("scrape_ms") or 0)
        except (TypeError, ValueError):
            scrape_ms = 0.0
        person = msg.get("person") or job.get("person_id") or ""
        url = msg.get("url") or job_url(job)
        message = msg.get("message")
        final_url = msg.get("final_url") or ""
        if _has_captcha(error, url, message, final_url):
            if not _has_captcha(error if isinstance(error, str) else ""):
                error = _captcha_note(error, str(final_url or url or ""))
            bucket = "rate_limit"
        if bucket == "rate_limit":
            self._on_rate_limit(slot, job, msg, error, person, url)
            return
        if bucket == "no_phone":
            self._finish(job, "parse_fail", error="no_phone", retry=False)
            print(f"  [quality_fail] job={jid} reason=no_phone moved to DLQ")
            return
        if scrape_ms:
            _observe(self.m, "scrape_ms", scrape_ms)
        if bucket == "success":
            self._note_success()
        if bucket in _ACK_BUCKETS:
            self._group_of(slot.slot).consecutive_rate_limits = 0
            self._finish(job, bucket)
            print(f"  [{bucket}] job={jid} scrape={scrape_ms:.0f}ms")
            return
        self._finish(job, bucket, error=bucket, retry=True)
        print(f"  [{bucket}] job={jid} scrape={scrape_ms:.0f}ms")

    def _pause_remaining_sec(self) -> int:
        return max(0, int(self._claim_after - time.monotonic()))

    def _heartbeat_status(self) -> str:
        if self.stop.is_set():
            return "stopping"
        if self._pause_remaining_sec() > 0:
            return "paused"
        return "running"

    def _is_dynamic_group(self, group: Optional[_ChromeGroup] = None) -> bool:
        """判断当前槽位或环境是否使用动态住宅代理（每次请求不同IP），若是则不需要任何冷却。"""
        if os.environ.get("NO_RATE_LIMIT_COOLDOWN") == "1" or os.environ.get("TPS_NO_COOLDOWN") == "1":
            return True
        if os.environ.get("RATE_LIMIT_PAUSE_SEC", "").strip() in ("0", "none", "false"):
            return True
        target = group if group is not None else (self.groups[0] if self.groups else None)
        proxy = target.proxy if target else None
        if proxy:
            return is_dynamic_proxy(proxy)
        return False

    def _note_rate_limit(self, group: Optional[_ChromeGroup] = None) -> int:
        """同一轮限流里后续 429 不加大暂停。动态代理模式直接返回 0。"""
        if self._is_dynamic_group(group):
            self._claim_after = 0.0
            self._rate_limit_streak = 0
            return 0
        now = time.monotonic()
        if now < self._claim_after:
            return self._pause_remaining_sec()
        self._rate_limit_streak += 1
        pause = rate_limit_pause_sec(self._rate_limit_streak)
        self._claim_after = now + pause
        return pause

    def _note_success(self) -> None:
        if time.monotonic() >= self._claim_after:
            self._rate_limit_streak = 0

    def _group_of(self, slot_id: int) -> _ChromeGroup:
        for group in self.groups:
            if any(slot.slot == slot_id for slot in group.slots):
                return group
        return self.groups[0]

    def _group_resting(self, group: _ChromeGroup) -> bool:
        return time.monotonic() < group.claim_after

    def _on_rate_limit(self, slot: _BrowserSlot, job: dict, msg: dict, error, person: str, url: str) -> None:
        jid = job_id(job)
        with self.lock:
            if jid in self.abandoned:
                return
        group = self._group_of(slot.slot)
        generation = msg.get("generation")
        stale = generation is not None and int(generation) != group.generation
        switched = False
        if not stale:
            switched = self._rotate_region_gateway(group)

        is_dynamic = self._is_dynamic_group(group)

        if switched or is_dynamic:
            # 动态住宅代理（每次请求不同IP）：无需任何停顿，0秒冷却直接以新IP恢复抓取！
            group.claim_after = 0.0
            pause = 0.0
            self._claim_after = 0.0
            self._rate_limit_streak = 0
            label = ProxyManager._mask_proxy(group.proxy) if group.proxy else "dynamic"
            print(
                f"  [rate_limit] 槽位 chrome={group.gid} 遇到风控 -> ⚡ 动态住宅代理(每次请求不同IP): {label}，零冷却立即重试！",
                flush=True,
            )
        else:
            # 仅在无法轮换的固定 IP 模式下执行阶梯退避
            pause = self._note_rate_limit(group)
            print(f"  [rate_limit] job={jid} pause={pause}s returned to pending (固定IP冷却)", flush=True)

        try:
            release(self.r, job, "rate_limited")
            _incr(self.m, "rate_limit")
        except Exception as exc:
            print(f"[QUEUE] release {jid}: {exc}", file=sys.stderr)
        self._proc_heartbeat()

    def _arm_proxy(self, group: _ChromeGroup, proxy: str) -> None:
        group.proxy = proxy
        group.generation += 1
        group.claim_after = 0.0
        if group.proc is not None and group.proc.is_alive():
            group.swapping = True
            self._signal_proc(group.proc, signal.SIGTERM)

    def _rotate_region_gateway(self, group: _ChromeGroup) -> bool:
        """动态代理换 sid 或穿云动态网关换出口。"""
        current = group.proxy or ""
        if not current:
            return False
        user = _proxy_username(current).lower()
        host = (urlparse(current if "://" in current else "http://" + current).hostname or "").lower()
        if "cloudbypass" in host or "gw-res" in host or "-res_" in user or "res_us" in user:
            refreshed = refresh_sticky_url(current)
            if refreshed and refreshed != current:
                self._arm_proxy(group, refreshed)
            return True
        if "-region-" in user:
            refreshed = refresh_sticky_url(current)
            if not refreshed or refreshed == current:
                return False
            self._arm_proxy(group, refreshed)
            return True
        return False

    def _switch_ip(self, group: _ChromeGroup, region_checked: bool = False) -> bool:
        if not region_checked and self._rotate_region_gateway(group):
            return True
        if self.lanes is None:
            return False
        nxt = self.lanes.cool(str(group.gid))
        if not nxt:
            return False
        self._arm_proxy(group, nxt)
        return True

    def _claim_one(self) -> None:
        if self.stop.is_set() or not self.idle:
            return
        if (_ROOT_DIR / "data" / "client.pause").exists():
            if not getattr(self, "_client_pause_logged", False):
                print("[PAUSE] 客户端已暂停，不领取新任务", flush=True)
                self._client_pause_logged = True
            return
        self._client_pause_logged = False
        if self._pause_remaining_sec() > 0:
            self._log_idle()
            return
        usable = [
            slot_id for slot_id in self.idle
            if not self._group_resting(self._group_of(slot_id))
        ]
        if not usable:
            self._log_idle()
            return
        try:
            pending = int(self.r.llen(PENDING_KEY) or 0)
        except Exception:
            pending = 1
        if pending <= 0:
            self._log_idle()
            return
        slot_id = usable[-1]
        self.idle.remove(slot_id)
        try:
            job = claim(self.r, self.worker_id)
        except Exception as exc:
            self.idle.add(slot_id)
            print(f"[CLAIM] {exc}", file=sys.stderr)
            time.sleep(0.5)
            return
        if not job:
            self.idle.add(slot_id)
            self._log_idle()
            return
        if self.stop.is_set():
            self._nack_shutdown(job)
            self.idle.add(slot_id)
            return
        self._assign(slot_id, job)

    def _assign(self, slot_id: int, job: dict) -> None:
        slot = self.slots[slot_id]
        jid = job_id(job)
        url = job_url(job)
        with self.lock:
            self.in_flight[jid] = job
        slot.job = job
        slot.jid = jid
        _observe_queue_wait(self.m, job)
        if not url:
            with self.lock:
                self.in_flight.pop(jid, None)
            slot.job = None
            slot.jid = ""
            self._finish(job, "parse_fail", error="missing url", retry=True)
            self.idle.add(slot_id)
            return
        try:
            heartbeat(self.r, job)
        except Exception as exc:
            print(f"[HB] job {jid}: {exc}", file=sys.stderr)
        try:
            slot.in_q.put(job, timeout=2)
        except Exception as exc:
            with self.lock:
                self.in_flight.pop(jid, None)
            slot.job = None
            slot.jid = ""
            self._finish(job, "retry", error="dispatch_failed", retry=True)
            self.idle.add(slot_id)
            return
        self._proc_heartbeat()

    def _nack_shutdown(self, job: dict) -> None:
        try:
            nack(self.r, job, "worker shutdown", retry=True)
            _incr(self.m, "retry")
            print(f"  [nack/shutdown] job={job_id(job)}")
        except Exception as exc:
            print(f"[NACK] {exc}", file=sys.stderr)

    def _log_idle(self) -> None:
        now = time.monotonic()
        if now - self._last_idle < IDLE_LOG_SEC:
            return
        self._last_idle = now
        remain = self._pause_remaining_sec()
        try:
            qs = queue_stats(self.r)
            if remain > 0:
                print(
                    f"[WAIT] rate-limit pause {remain}s "
                    f"pending={qs.get('pending', 0)} "
                    f"processing={qs.get('processing', 0)} "
                    f"dlq={qs.get('dlq', 0)} "
                    "(warm browsers stay up)"
                )
            else:
                print(
                    f"[WAIT] pending={qs.get('pending', 0)} "
                    f"processing={qs.get('processing', 0)} "
                    f"dlq={qs.get('dlq', 0)} "
                    "(no new jobs; warm browsers stay up)"
                )
        except Exception:
            if remain > 0:
                print(f"[WAIT] rate-limit pause {remain}s")
            else:
                print("[WAIT] pending empty")

    def _reap_slots(self) -> None:
        for group in self.groups:
            proc = group.proc
            if proc is None or proc.is_alive():
                continue
            code = proc.exitcode
            for slot in group.slots:
                self.idle.discard(slot.slot)
                job = slot.job
                jid = slot.jid
                slot.job = None
                slot.jid = ""
                if job is None:
                    continue
                with self.lock:
                    self.in_flight.pop(jid or job_id(job), None)
                if group.swapping:
                    try:
                        release(self.r, job, "proxy rotated")
                    except Exception as exc:
                        print(f"[QUEUE] release {jid or job_id(job)}: {exc}", file=sys.stderr)
                else:
                    self._finish(job, "retry", error=f"browser exited {code}", retry=True)
            if self.stop.is_set():
                continue
            if group.swapping:
                group.swapping = False
                print(
                    f"[PROXY] chrome={group.gid} restarted on "
                    f"{ProxyManager._mask_proxy(group.proxy) if group.proxy else 'direct'}",
                    flush=True,
                )
                group.start(self.out_q)
                continue
            group.restarts += 1
            if group.restarts > 8:
                print(f"[BROWSER] chrome={group.gid} restarted too often, stopping", file=sys.stderr)
                self.stop.set()
                return
            print(f"[BROWSER] chrome={group.gid} exited code={code}, restart")
            time.sleep(0.5)
            group.start(self.out_q)

    def _stop_browsers(self) -> None:
        seen = set()
        procs = []
        for group in self.groups:
            proc = group.proc
            if proc is None or proc.pid is None or proc.pid in seen:
                continue
            seen.add(proc.pid)
            procs.append(proc)
            self._signal_proc(proc, signal.SIGTERM)
        for proc in procs:
            proc.join(timeout=2)
            if proc.is_alive():
                self._signal_proc(proc, _SIGKILL)
                proc.join(timeout=1)

    def _signal_proc(self, proc, sig: int) -> None:
        if proc is None or proc.pid is None or not proc.is_alive():
            return
        if psutil is not None:
            try:
                p = psutil.Process(proc.pid)
                children = p.children(recursive=True)
                for child in children:
                    try:
                        child.send_signal(sig)
                    except (OSError, psutil.Error):
                        pass
                p.send_signal(sig)
                return
            except (OSError, psutil.Error):
                pass
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            try:
                os.kill(proc.pid, sig)
            except OSError:
                pass

    def _is_abandoned(self, jid: str) -> bool:
        with self.lock:
            return jid in self.abandoned

    def _finish(
        self,
        job: dict,
        bucket: str,
        error: Optional[str] = None,
        retry: bool = True,
    ) -> None:
        jid = job_id(job)
        with self.lock:
            if jid in self.abandoned or jid in self.finalized:
                return
            self.finalized.add(jid)

        try:
            if bucket in _ACK_BUCKETS:
                _incr(self.m, bucket)
                ack(self.r, job)
                return

            _incr(self.m, bucket)
            attempts = int(job.get("attempts") or 0)
            if not retry or attempts + 1 >= int(MAX_ATTEMPTS):
                _incr(self.m, "dlq")
            elif bucket != "retry":
                _incr(self.m, "retry")
            nack(self.r, job, error or bucket, retry=retry)
        except Exception as exc:
            print(f"[QUEUE] finish {bucket} {jid}: {exc}", file=sys.stderr)

    def _job_runtime_sec(self, job: dict) -> float:
        claimed = job.get("claimed_at") or job.get("enqueued_at")
        try:
            claimed = float(claimed)
        except (TypeError, ValueError):
            return 0.0
        if claimed > 1e12:
            claimed = claimed / 1000.0
        return max(0.0, time.time() - claimed)

    def _heartbeat_loop(self) -> None:
        while not self.stop.wait(HB_INTERVAL_SEC):
            self._proc_heartbeat()
            with self.lock:
                jobs = list(self.in_flight.values())
            for job in jobs:
                # 卡太久就停续租，recover 会把任务救回 pending
                if self._job_runtime_sec(job) > LEASE_SEC * 2:
                    print(f"[HB] skip stale {job_id(job)} runtime={self._job_runtime_sec(job):.0f}s")
                    continue
                try:
                    heartbeat(self.r, job)
                except Exception as exc:
                    print(f"[HB] {job_id(job)}: {exc}", file=sys.stderr)

    def _recover_loop(self) -> None:
        while not self.stop.wait(RECOVER_INTERVAL_SEC):
            try:
                n = recover_expired(self.r)
                if n:
                    print(f"[RECOVER] reclaimed={n}")
                dlq_count = int(self.r.llen(DLQ_KEY) or 0)
                if dlq_count > 50:
                    try:
                        import tps_alert
                        tps_alert.send_alert(
                            "死信队列(DLQ)积压预警",
                            f"死信队列 {DLQ_KEY} 已积压 {dlq_count} 条失败任务，请检查代理池连通性与风控状态。",
                            level="ERROR",
                        )
                    except Exception:
                        pass
            except Exception as exc:
                print(f"[RECOVER] {exc}", file=sys.stderr)

    def _wait_in_flight(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain_results(block=0.2)
            with self.lock:
                n = len(self.in_flight)
            if n == 0:
                return True
        return False

    def _abandon_remaining(self) -> None:
        with self.lock:
            leftover = []
            for jid, job in self.in_flight.items():
                if jid not in self.finalized:
                    self.abandoned.add(jid)
                    leftover.append(job)
        for job in leftover:
            try:
                nack(self.r, job, "worker shutdown", retry=True)
                _incr(self.m, "retry")
                print(f"  [nack/shutdown] job={job_id(job)}")
            except Exception as exc:
                print(f"[NACK] shutdown {job_id(job)}: {exc}", file=sys.stderr)

    def _shutdown(self) -> None:
        self.stop.set()
        print(f"[SHUTDOWN] waiting in-flight up to {SHUTDOWN_WAIT_SEC:.0f}s")
        finished = self._wait_in_flight(SHUTDOWN_WAIT_SEC)
        self._drain_results(block=0.2)
        if not finished:
            with self.lock:
                n = len(self.in_flight)
            print(f"[SHUTDOWN] timeout, nack leftover={n}")
        self._abandon_remaining()
        try:
            clear_worker_heartbeat(self.r, self.worker_id)
        except Exception:
            pass
        self._stop_browsers()
        print("[SHUTDOWN] done")


def run_feed(filepath: str) -> None:
    r = connect_redis()
    try:
        drained = drain_legacy(r)
        if drained:
            print(f"[DRAIN] legacy={drained}")
    except Exception as exc:
        print(f"[DRAIN] {exc}", file=sys.stderr)

    with open(filepath, encoding="utf-8") as fh:
        urls = []
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)

    stats = feed(r, urls, seen_check=True)
    enqueued = int(stats.get("enqueued") or 0)
    deduped = int(stats.get("deduped") or 0)
    invalid = int(stats.get("invalid") or 0)
    print(f"[FEED] enqueued={enqueued} deduped={deduped} invalid={invalid}")
    if deduped:
        try:
            metrics_client(r).incr("dedup_hit", deduped)
        except TypeError:
            for _ in range(deduped):
                _incr(metrics_client(r), "dedup_hit")
        except Exception as exc:
            print(f"[METRICS] dedup_hit: {exc}", file=sys.stderr)


def run_recover() -> None:
    r = connect_redis()
    n = recover_expired(r)
    print(f"[RECOVER] reclaimed={n}")


def run_stats() -> None:
    r = connect_redis()
    qs = queue_stats(r)
    snap = metrics_client(r).snapshot()
    print("[STATS] queue " + json.dumps(qs, ensure_ascii=False, default=str))
    print("[STATS] metrics " + json.dumps(snap, ensure_ascii=False, default=str))


def load_worker_lanes(proxy_file: str = None, concurrency: int = 2, proxy_tunnel: str = None):
    """粘性 IP 来自代理文件或隧道网关拆分。"""
    if proxy_file:
        lanes = StickyLanes.from_file(proxy_file)
        print(f"[PROXY] sticky file lanes={lanes.count} rest={int(lanes.rest_sec)}s", flush=True)
        return lanes
    tunnel = proxy_tunnel or os.environ.get("PROXY_TUNNEL")
    if tunnel:
        lane_count = max(2, int(concurrency or 2))
        lanes = tunnel_sticky_lanes(tunnel, count=lane_count)
        if lanes is not None:
            print(
                f"[PROXY] sticky tunnel lanes={lanes.count} rest={int(lanes.rest_sec)}s",
                flush=True,
            )
            return lanes
    try:
        cfg = load_proxy_config()
    except Exception as exc:
        print(f"[PROXY] config unread: {exc}", file=sys.stderr)
        return None
    mode = (cfg.get("mode") or "direct").lower()
    if mode == "tunnel" and cfg.get("tunnel"):
        lane_count = max(2, int(concurrency or 2))
        lanes = tunnel_sticky_lanes(cfg["tunnel"], count=lane_count)
        if lanes is not None:
            print(
                f"[PROXY] sticky tunnel lanes={lanes.count} rest={int(lanes.rest_sec)}s",
                flush=True,
            )
            return lanes
    lanes = sticky_lanes_from_config(cfg)
    if lanes is not None:
        print(f"[PROXY] sticky file lanes={lanes.count} rest={int(lanes.rest_sec)}s", flush=True)
    return lanes


def run_worker(concurrency: int, target_per_day: int, page_sec: float, proxy_file: str = None, proxy_tunnel: str = None) -> None:
    r = connect_redis()
    LeaseWorker(
        r,
        concurrency,
        target_per_day,
        page_sec,
        lanes=load_worker_lanes(proxy_file, concurrency=concurrency, proxy_tunnel=proxy_tunnel),
    ).run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TruePeopleSearch distributed worker (lease queue, no LPOP)",
    )
    parser.add_argument(
        "--mode",
        choices=["feed", "worker", "recover", "stats"],
        default="worker",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="long-lived browsers in this process (default: host memory budget)",
    )
    parser.add_argument(
        "--target-per-day",
        type=int,
        default=DAILY_TARGET,
        help="steady-state pages/day the scale log plans for (default 3000000)",
    )
    parser.add_argument(
        "--page-sec",
        type=float,
        default=WARM_PAGE_SEC,
        help="planned seconds per page on a warm browser (default 8)",
    )
    parser.add_argument("--file", help="URL list file (feed mode)")
    parser.add_argument(
        "--proxy-file",
        default=os.environ.get("PROXY_FILE"),
        help="sticky proxy list, one URL per line; each Chrome keeps one IP until HTTP 429",
    )
    parser.add_argument(
        "--proxy-tunnel",
        default=os.environ.get("PROXY_TUNNEL"),
        help="residential tunnel gateway, e.g. http://username:password@proxy.example.invalid:8080",
    )
    args = parser.parse_args()

    if args.mode == "feed":
        if not args.file:
            parser.error("--file is required for feed mode")
        run_feed(args.file)
    elif args.mode == "recover":
        run_recover()
    elif args.mode == "stats":
        run_stats()
    else:
        if args.page_sec <= 0:
            parser.error("--page-sec must be > 0")
        plan = resolve_browsers(args.concurrency, args.target_per_day, args.page_sec)
        if plan["clamped"]:
            print(
                f"[SCALE] --concurrency {args.concurrency} clamped to {plan['browsers']}",
                file=sys.stderr,
            )
        run_worker(
            plan["browsers"],
            plan["per_day_target"],
            plan["page_sec"],
            args.proxy_file,
            proxy_tunnel=args.proxy_tunnel,
        )


if __name__ == "__main__":
    main()
