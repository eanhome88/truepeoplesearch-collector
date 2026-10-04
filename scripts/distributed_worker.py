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
有备用独立出口只换道不停全局；无备用才全局等；验证码只重刷指纹。
用户名含 -region- 的粘性网关换新 sid（新出口）并重启 Chrome。
InternalCaptcha / captcha 页同样 release，不按空页 ACK。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
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
try:
    import cf_challenge
except ImportError:  # 客户机旧包缺文件时降级：分型缺失不炸主流程
    cf_challenge = None
from proxy_pool import (
    ProxyManager,
    StickyLanes,
    fetch_proxy_list,
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
    FETCH_TIMEOUT_MS,
    ensure_db,
    fetch_cloudbypass_v2,
    fetch_document,
    fetch_in_async_session,
    html_page,
    ingest_response,
    is_challenge_html,
    open_async_stealth_session,
)
import scrape_to_tidb as _scrape_mod
from cf_solver import (
    CfSolver,
    CfSolverError,
    DEFAULT_USER_AGENT,
    impersonate_for_ua,
    publish_warmed,
    require_cf_solver,
    sid_for_proxy,
)

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
try:
    SESSION_RECYCLE_PAGES = int(os.environ.get("TPS_SESSION_RECYCLE", "120"))
    if SESSION_RECYCLE_PAGES < 20:
        SESSION_RECYCLE_PAGES = 20
except (TypeError, ValueError):
    SESSION_RECYCLE_PAGES = 120
SHUTDOWN_WAIT_SEC = 6
# 自研过 CF：不开浏览器，凭证来自 TPS_CF_SOLVER，请求走 curl_cffi 同代理同指纹。
OWN_CF_BYPASS = os.environ.get("TPS_OWN_CF", "0") == "1"
# 指纹/验证码死亡本组短休秒数，全局不停。
FP_REST_SEC = float(os.environ.get("TPS_FP_REST_SEC", "45"))
# 换到备用出口后本组热身秒数。
LANE_WARM_SEC = float(os.environ.get("TPS_LANE_WARM_SEC", "15"))
# 固定出口 429：5 / 15 / 45 分钟。换 IP 不能缩短这段。
RATE_LIMIT_PAUSE_STEPS_SEC = (300, 900, 2700)
# 动态住宅（穿云箭等）429 是账号总量，不是单个 IP。换出口，但全账号短暂停：
# 20 秒、40 秒，封顶 60 秒。成功后连击清零。
ACCOUNT_PAUSE_STEPS_SEC = (20, 40, 60)
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

    # 代理层网络错误：明确进 retry（换组换 proxy 重排），且排在 "empty"+"page"
    # 误判之前——文案里带 empty 但不是空页。
    if any(s in msg for s in ("err_empty_response", "err_connection", "err_socket",
                              "err_proxy", "err_timed_out", "connection reset",
                              "connection closed", "empty response", "broken pipe",
                              "net_retry", "net::")):
        return "retry"
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
            label = ProxyManager._mask_proxy(self.proxy) if self.proxy else "direct"
            if OWN_CF_BYPASS:
                solver = _own_cf_solver()
                self.session = _OwnCfSession(self.proxy, solver)
                print(
                    f"[OWN_CF] protocol session tabs={self.tabs} proxy={label} solver={solver.describe()}",
                    flush=True,
                )
            else:
                self.session = await open_async_stealth_session(self.tabs, proxy=self.proxy)
                print(f"[BROWSER] chrome open tabs={self.tabs} proxy={label}", flush=True)
        return self.session


_OWN_CF_SOLVER: Optional[CfSolver] = None


def _own_cf_solver() -> CfSolver:
    """每个浏览器组进程一个求解器实例；加载失败直接抛，整组报 fatal。"""
    global _OWN_CF_SOLVER
    if _OWN_CF_SOLVER is None:
        _OWN_CF_SOLVER = require_cf_solver()
    return _OWN_CF_SOLVER


class _OwnCfSession:
    """TPS_OWN_CF=1 的会话：没有浏览器。凭证来自自研求解器，请求用 curl_cffi 走同一代理，
    TLS 指纹按凭证里的 UA 版本挑。一个会话被组里所有标签页共用，重解靠 solves 代数去重。"""

    def __init__(self, proxy: Optional[str], solver: CfSolver):
        self.proxy = proxy or None
        self.solver = solver
        self.client = None
        self.user_agent = ""
        self.cookies: dict = {}
        self.pending_html: dict = {}
        self.solves = 0
        self.impersonate = ""

    async def open(self, url: str) -> None:
        solution = await self.solver.solve(url, proxy=self.proxy, user_agent=None)
        await self._apply(solution, url)

    async def resolve(self, url: str) -> None:
        """凭证被拒后重解。沿用 UA，否则 cf_clearance 和 UA 对不上等于白解。"""
        solution = await self.solver.solve(url, proxy=self.proxy, user_agent=self.user_agent or None)
        await self._apply(solution, url)

    async def _apply(self, solution, url: str) -> None:
        ua = solution.user_agent or self.user_agent or DEFAULT_USER_AGENT
        target = impersonate_for_ua(ua)
        if self.client is None or target != self.impersonate:
            await self._close_client()
            self.client = self._new_client(target)
            self.impersonate = target
        self.user_agent = ua
        if solution.cookies:
            self.cookies.update(solution.cookies)
            try:
                self.client.cookies.update(solution.cookies)
            except Exception:
                pass
        if solution.html:
            self.pending_html[url] = solution.html
        self.solves += 1
        label = ProxyManager._mask_proxy(self.proxy) if self.proxy else "direct"
        print(
            f"[OWN_CF] clearance #{self.solves} cookies={len(solution.cookies)} "
            f"ua=Chrome/{_chrome_major(ua)} impersonate={target} proxy={label}",
            flush=True,
        )
        publish_warmed(url, solution, sid=sid_for_proxy(self.proxy))

    def _new_client(self, impersonate: str):
        from curl_cffi.requests import AsyncSession

        kwargs = {"impersonate": impersonate, "timeout": FETCH_TIMEOUT_MS / 1000.0}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return AsyncSession(**kwargs)

    async def get(self, url: str) -> tuple:
        html = self.pending_html.pop(url, None)
        if html is not None:
            return 200, html
        if self.client is None:
            raise RuntimeError("own-cf session has no client; call open() first")
        headers = {
            "user-agent": self.user_agent or DEFAULT_USER_AGENT,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "referer": "https://www.google.com/",
            "upgrade-insecure-requests": "1",
        }
        if self.cookies:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items() if k)
        resp = await self.client.get(
            url, headers=headers, timeout=FETCH_TIMEOUT_MS / 1000.0, allow_redirects=True,
        )
        return int(resp.status_code), resp.text or ""

    async def _close_client(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def close(self) -> None:
        await self._close_client()
        self.pending_html.clear()


def _chrome_major(ua: str) -> str:
    import re as _re

    m = _re.search(r"Chrome/(\d+)", ua or "")
    return m.group(1) if m else "?"


async def _fetch_page_own_cf(box: _ChromeBox, url: str):
    """自研过 CF 路径：首页先要凭证；拿着凭证发协议请求；被挑战就重解一次，
    再被拒就按验证码上报（同出口刷凭证，连续两次换出口），交给 LeaseWorker 的限流逻辑。"""
    async with box.gate:
        session = await box.ensure_session()
        if not box.warm:
            try:
                await session.open(url)
            except CfSolverError as exc:
                # 拿不到首份凭证按验证码上报：任务 release 不计 attempts，本组休息后同出口再要，连续两次换出口。
                print(f"[OWN_CF] solver failed on first clearance: {exc}", flush=True)
                raise HttpError(
                    503, f"solver failed on first clearance (captcha) for {url}: {exc}",
                    bucket="rate_limit",
                ) from exc
            box.warm = True
    for attempt in (1, 2):
        generation = session.solves
        try:
            status, body = await session.get(url)
        except Exception as exc:
            msg = str(exc).lower()
            if "timeout" in msg or "timed out" in msg:
                raise _scrape_mod.FetchTimeoutError(f"timeout {FETCH_TIMEOUT_MS}ms for {url}") from exc
            raise
        if status == 429:
            raise HttpError(429, f"HTTP 429 for {url}")
        if status in (404, 410):
            return html_page(body, status, url)
        challenged = status in (403, 503) or is_challenge_html(body)
        if challenged and cf_challenge is not None:
            try:
                _kind = cf_challenge.classify(url=url, html=body, status=status)
                print(f"[OWN_CF] kind={_kind} route={cf_challenge.first_action(_kind)} (HTTP {status})", flush=True)
            except Exception:
                pass
        if status == 200 and not challenged:
            return html_page(body, status, url)
        if not challenged:
            raise HttpError(status, f"HTTP {status} for {url}")
        if attempt == 1:
            async with box.gate:
                if session.solves == generation:
                    print(f"[OWN_CF] clearance rejected (HTTP {status}); asking solver again", flush=True)
                    try:
                        await session.resolve(url)
                    except CfSolverError as exc:
                        raise HttpError(
                            status, f"solver failed after challenge (captcha) for {url}: {exc}",
                            bucket="rate_limit",
                        ) from exc
            continue
    raise HttpError(
        status, f"cloudflare challenge persists after re-solve (captcha) for {url}",
        bucket="rate_limit",
    )


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
    # 反锁步抖动：32 个 tab 同时起抓是明显的机器节拍，加 slight 随机错峰。
    # TPS_FETCH_JITTER_MS="200,800" 默认；"0,0" 关闭。
    try:
        _jlo, _jhi = _scrape_mod.parse_jitter_ms()
    except Exception:
        _jlo, _jhi = (0, 0)
    if _jhi > 0:
        await asyncio.sleep(random.uniform(_jlo, _jhi) / 1000.0)

    def done(bucket: str, error, served_ok: bool, page=None, extra=None) -> dict:
        final_url = _captcha_final_url(page, url)
        scan_visible = bucket == "empty" or _has_captcha(error)
        if _has_captcha(error, url, final_url) or _looks_like_captcha(page, url, include_visible=scan_visible):
            if not _has_captcha(error):
                error = _captcha_note(error, final_url or url)
            bucket = "rate_limit"
            served_ok = False
        if served_ok:
            box.served += 1
        msg = {
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
        if extra:
            msg.update(extra)
        return msg

    if not url:
        return done("parse_fail", "missing url", False)

    last_bucket = "retry"
    last_error = "fetch failed"
    last_kind = ""
    last_route = ""
    page = None

    def _cf_kind(exc, pg) -> tuple:
        """分型+首选动作：纯观测，不改变 bucket 语义，异常时回空。"""
        if cf_challenge is None:
            return "", ""
        try:
            kind = cf_challenge.classify_exception(exc, url, pg)
            return kind, cf_challenge.first_action(kind)
        except Exception:
            return "", ""
    for attempt in (1, 2):
        fetched = None
        try:
            fetched = await _fetch_page(box, url)
            page = fetched
            if _looks_like_captcha(page, url):
                kind, route = _cf_kind(RuntimeError("captcha page"), page)
                return done("rate_limit", None, False, page, extra={
                    "cf_kind": kind or "site_captcha", "cf_route": route,
                })
            async with box.db_lock:
                box.db = await asyncio.to_thread(ensure_db, box.db)
                data = await asyncio.to_thread(ingest_response, page, url, box.db)
            box.warm = True
            # 搜索页 fan-out（只注回人物链接、未写 persons 行）与真正入库分开计数，
            # 面板 PERSONS 只认 DB 行，success 不再虚胖。
            is_fanout = isinstance(data, dict) and bool(data.get("is_search_result"))
            has_db_row = isinstance(data, dict) and bool(data.get("person_id")) and not is_fanout
            bucket = "success" if data else "empty"
            return done(bucket, None, True, page, extra={
                "fanout": int(data.get("count") or 0) if is_fanout else 0,
                "fanout_hit": bool(is_fanout),
                "db_write": bool(has_db_row),
            })
        except Exception as exc:
            page = fetched
            last_error = _short_err(exc)
            last_bucket = classify_error(exc, url=url, page=page)
            last_kind, last_route = _cf_kind(exc, page)
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
    return done(last_bucket, last_error, False, page, extra={
        "cf_kind": last_kind, "cf_route": last_route,
    })


async def _fetch_page(box: _ChromeBox, url: str):
    """自研过 CF 时走求解器+协议；否则优先穿云 V2 网关，失败退回本地协议与浏览器渲染。"""
    if OWN_CF_BYPASS:
        return await _fetch_page_own_cf(box, url)
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
        self._proxy_born = {}
        opened = time.time()
        proxy_file = (os.environ.get("PROXY_FILE") or "").strip()
        if proxy_file:
            try:
                opened = os.path.getmtime(proxy_file)
            except OSError:
                pass
        for gid, size in enumerate(sizes):
            group = _ChromeGroup(gid, self.slots[start:start + size], self.ctx)
            if self.lanes is not None:
                group.proxy = self.lanes.checkout(str(gid))
                self._proxy_born[gid] = opened
            groups.append(group)
            start += size
        self.groups = groups
        self.idle: set[int] = set()
        self._last_idle = 0.0
        self._claim_after = 0.0
        self._next_claim_at = 0.0
        self._rate_limit_streak = 0
        self._recent: list[int] = []
        self._consec_success = 0
        try:
            self._lanes_shared_host = bool(lanes.shared_host) if lanes is not None else True
        except Exception:
            self._lanes_shared_host = True

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
        if os.environ.get("PROXY_API_URL", "").strip():
            threading.Thread(
                target=self._api_refresh_loop, name="tps-proxy-refresh", daemon=True,
            ).start()
        threading.Thread(
            target=self._sticky_refresh_loop, name="tps-sticky-refresh", daemon=True,
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
        self._record_outcome(bucket == "rate_limit")
        cf_kind = str(msg.get("cf_kind") or "")
        if cf_kind and cf_challenge is not None:
            cf_challenge.note_challenge(self.r, cf_kind)
            print(f"  [CF_KIND] job={jid} kind={cf_kind} route={msg.get('cf_route') or ''} bucket={bucket}")
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
            # 人物行真实入库才记 db_write；搜索页 fan-out 只记 fanout，不再虚增成功含金量。
            try:
                if msg.get("db_write"):
                    _incr(self.m, "db_write")
                if msg.get("fanout_hit"):
                    _incr(self.m, "fanout")
            except Exception:
                pass
        if bucket in _ACK_BUCKETS:
            self._group_of(slot.slot).consecutive_rate_limits = 0
            self._finish(job, bucket)
            print(f"  [{bucket}] job={jid} scrape={scrape_ms:.0f}ms")
            return
        self._finish(job, bucket, error=(error if isinstance(error, str) and error else bucket), retry=True)
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
        """判断当前槽位或环境是否使用动态住宅代理（每次请求不同IP），隧道单网关 429 走账号总量暂停。"""
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

    def _note_account_limit(self) -> int:
        """动态 IP 的 429：换出口解决不了账号总量，全员按 20/40/60 秒暂停。"""
        now = time.monotonic()
        if now < self._claim_after:
            return self._pause_remaining_sec()
        self._rate_limit_streak += 1
        idx = min(max(int(self._rate_limit_streak), 1), len(ACCOUNT_PAUSE_STEPS_SEC)) - 1
        pause = ACCOUNT_PAUSE_STEPS_SEC[idx]
        self._claim_after = now + pause
        return pause

    def _record_outcome(self, limited: bool) -> None:
        self._recent.append(1 if limited else 0)
        if len(self._recent) > 100:
            self._recent = self._recent[-100:]

    def _claim_gap_sec(self) -> float:
        raw = os.environ.get("TPS_CLAIM_GAP_SEC", "3")
        try:
            base = float(raw)
        except (TypeError, ValueError):
            base = 3.0
        if self._recent:
            ratio = sum(self._recent) / len(self._recent)
            extra = min(12.0, ratio * 12.0)
        else:
            extra = 0.0
        jitter = random.uniform(0, max(0.5, base * 0.25))
        return min(18.0, base + extra + jitter)

    def _note_success(self) -> None:
        self._consec_success += 1
        if self._consec_success >= 5:
            self._rate_limit_streak = 0
            self._consec_success = 0
        else:
            self._rate_limit_streak = max(0, self._rate_limit_streak - 2)

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
        now = time.monotonic()
        self._consec_success = 0
        label = ProxyManager._mask_proxy(group.proxy) if group.proxy else "direct"
        if _has_captcha(error, url):
            group.consecutive_rate_limits += 1
            if group.consecutive_rate_limits >= 2:
                self._arm_proxy(group, group.proxy)
                action = "同出口重开浏览器刷指纹"
            else:
                action = "先休不重开"
            rest = FP_REST_SEC + random.uniform(0, 10)
            group.claim_after = now + rest
            print(
                f"  [fp_restart] chrome={group.gid} 指纹/验证码死亡{action}，本组休{rest:.0f}s，全局不停: {label}",
                flush=True,
            )
        else:
            before = group.proxy or ""
            switched = False if stale else self._switch_ip(group)
            after = group.proxy or ""
            minted = after != before and ("gw-res" in after.lower() or "cloudbypass" in after.lower())
            many = self.lanes is not None and self.lanes.count > 1
            if minted and many:
                group.consecutive_rate_limits += 1
                warm = LANE_WARM_SEC + random.uniform(0, 5)
                group.claim_after = time.monotonic() + warm
                label = ProxyManager._mask_proxy(group.proxy) if group.proxy else "direct"
                print(
                    f"  [session_refresh] chrome={group.gid} 429后换新粘性会话，本组热身{warm:.0f}s，其他组不停: {label}",
                    flush=True,
                )
            elif (
                switched
                and self.lanes is not None
                and self.lanes.count > 1
                and not self._lanes_shared_host
                and not self._is_dynamic_group(group)
            ):
                group.consecutive_rate_limits += 1
                warm = LANE_WARM_SEC + random.uniform(0, 5)
                group.claim_after = time.monotonic() + warm
                label = ProxyManager._mask_proxy(group.proxy) if group.proxy else "direct"
                print(
                    f"  [lane_switch] chrome={group.gid} 429换到备用出口，本组热身{warm:.0f}s，全局不停: {label}",
                    flush=True,
                )
            elif self._is_dynamic_group(group):
                pause = self._note_account_limit()
                print(
                    f"  [rate_limit] 槽位 chrome={group.gid} 动态网关账号暂停 {pause}s: {label}",
                    flush=True,
                )
            elif self.lanes is not None and self.lanes.count > 1:
                try:
                    soonest = float(self.lanes.seconds_until_ready())
                except Exception:
                    soonest = 0.0
                if now < self._claim_after:
                    pause = self._pause_remaining_sec()
                else:
                    self._rate_limit_streak += 1
                    pause = min(max(soonest, 30.0), 600.0)
                    self._claim_after = now + pause
                print(f"  [lane_wait] 全部出口冷却中，全局等{pause:.0f}s", flush=True)
            else:
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
        if not hasattr(self, "_proxy_born"):
            self._proxy_born = {}
        self._proxy_born[group.gid] = time.time()
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
        if time.monotonic() < self._next_claim_at:
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
        self._next_claim_at = time.monotonic() + self._claim_gap_sec()
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

    def _sticky_refresh_loop(self) -> None:
        """穿云时效会话 30 分钟失效。满 25 分钟就地换新会话，不用再去面板提取。"""
        while not self.stop.wait(60):
            now = time.time()
            born = getattr(self, "_proxy_born", {})
            for group in list(self.groups):
                proxy = group.proxy or ""
                if "gw-res" not in proxy.lower() and "cloudbypass" not in proxy.lower():
                    continue
                if now - float(born.get(group.gid, now)) < 25 * 60:
                    continue
                fresh = refresh_sticky_url(proxy, minutes=30)
                if not fresh or fresh == proxy:
                    continue
                print(
                    f"[STICKY] chrome={group.gid} 会话将满 30 分钟，已换新会话",
                    flush=True,
                )
                self._arm_proxy(group, fresh)

    def _api_refresh_loop(self) -> None:
        """定时从上游拉新出口并热合并进 lanes，不停机、不丢任务。"""
        try:
            interval = max(120.0, float(os.environ.get("PROXY_API_REFRESH_SEC", "600")))
        except (TypeError, ValueError):
            interval = 600.0
        api_url = os.environ.get("PROXY_API_URL", "").strip()
        while not self.stop.wait(interval):
            if not api_url or self.lanes is None:
                continue
            try:
                urls = fetch_proxy_list(api_url)
            except Exception as exc:
                print(f"[PROXY_API] refresh failed: {exc}", file=sys.stderr)
                continue
            try:
                added = self.lanes.merge_urls(urls)
            except Exception as exc:
                print(f"[PROXY_API] merge failed: {exc}", file=sys.stderr)
                continue
            if added:
                print(f"[PROXY_API] merged +{added} fresh exits (pool={self.lanes.count})", flush=True)
            _save_proxy_api_cache(urls)

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


def _proxy_api_cache_path() -> Path:
    """上游拉取的出口列表本地缓存。data/ 在包外，永不进发布包。"""
    override = (os.environ.get("PROXY_API_CACHE") or "").strip()
    if override:
        return Path(override)
    candidates = (_ROOT_DIR / "data", _ROOT_DIR.parent / "data")
    for base in candidates:
        try:
            if base.is_dir():
                return base / "proxy_api_cache.txt"
        except OSError:
            continue
    return candidates[0] / "proxy_api_cache.txt"


def _save_proxy_api_cache(urls) -> None:
    try:
        path = _proxy_api_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[PROXY_API] cache save failed: {exc}", file=sys.stderr)


def _load_proxy_api_cache() -> list:
    try:
        lines = _proxy_api_cache_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip()]


def load_worker_lanes(proxy_file: str = None, concurrency: int = 2, proxy_tunnel: str = None, proxy_api_url: str = None):
    """粘性 IP 来自代理文件或隧道网关拆分。"""
    from proxy_pool import IP_REST_SEC as _DEFAULT_IP_REST

    def _ip_rest_sec() -> float:
        raw = os.environ.get("TPS_IP_REST_SEC", "")
        if not str(raw).strip():
            return float(_DEFAULT_IP_REST)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return float(_DEFAULT_IP_REST)
        if val < 60:
            return 60.0
        return val

    rest_sec = _ip_rest_sec()
    if proxy_file:
        lanes = StickyLanes.from_file(proxy_file, rest_sec=rest_sec)
        print(f"[PROXY] sticky file lanes={lanes.count} rest={int(lanes.rest_sec)}s", flush=True)
        return lanes
    api_url = (proxy_api_url or os.environ.get("PROXY_API_URL") or "").strip()
    if api_url:
        try:
            urls = fetch_proxy_list(api_url)
        except Exception as exc:
            print(f"[PROXY_API] initial pull failed: {exc}", file=sys.stderr)
            urls = []
        if not urls:
            urls = _load_proxy_api_cache()
            if urls:
                print(f"[PROXY_API] using cached exits lanes={len(urls)}", flush=True)
        if not urls:
            raise RuntimeError("PROXY_API_URL returned no exits and no cache exists; refusing to start without IPs.")
        _save_proxy_api_cache(urls)
        lanes = StickyLanes(urls, rest_sec=rest_sec)
        print(f"[PROXY] api lanes={lanes.count} rest={int(lanes.rest_sec)}s (refreshing in background)", flush=True)
        return lanes
    tunnel = proxy_tunnel or os.environ.get("PROXY_TUNNEL")
    if tunnel:
        lane_count = max(2, int(concurrency or 2))
        lanes = tunnel_sticky_lanes(tunnel, count=lane_count, rest_sec=rest_sec)
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
        lanes = tunnel_sticky_lanes(cfg["tunnel"], count=lane_count, rest_sec=rest_sec)
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


def run_worker(concurrency: int, target_per_day: int, page_sec: float, proxy_file: str = None, proxy_tunnel: str = None, proxy_api_url: str = None) -> None:
    if OWN_CF_BYPASS:
        # 启动期就把求解器加载一遍：没配、导不进、写法不对，这里直接死，不要跑起来才在子进程里静默失败。
        solver = require_cf_solver()
        print(f"[OWN_CF] 自研过CF模式：浏览器不参与，凭证来自 {solver.describe()}，请求走 curl_cffi 协议层", flush=True)
    r = connect_redis()
    LeaseWorker(
        r,
        concurrency,
        target_per_day,
        page_sec,
        lanes=load_worker_lanes(proxy_file, concurrency=concurrency, proxy_tunnel=proxy_tunnel, proxy_api_url=proxy_api_url),
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
    parser.add_argument(
        "--proxy-api-url",
        default=os.environ.get("PROXY_API_URL"),
        help="upstream provider API that returns fresh exits; pulled at startup and merged periodically",
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
            proxy_api_url=args.proxy_api_url,
        )


if __name__ == "__main__":
    main()
