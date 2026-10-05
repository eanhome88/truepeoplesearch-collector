#!/usr/bin/env python3
"""本地免费 Cloudflare 求解节点（FlareSolverr 兼容 /v1）。

只依赖标准库 + scrapling（StealthySession 同步版，solve_cloudflare=True），
给 scripts/cf_solver.py 的 flaresolverr:/byparr: 模式提供本机求解服务，不花钱。

契约（调用方已按此实现，本文件不得自创格式）：
  请求  POST /v1
    {"cmd": "request.get", "url": "...", "proxy": {"url": "..."} 可选,
     "maxTimeout": 毫秒}
  成功回
    {"status": "ok",
     "solution": {"cookies": [{"name": .., "value": ..}], "userAgent": ..}}
  失败回
    {"status": "error", "message": ..}
  无 cookie 也算失败（调用方要求）。

运行：
  python scripts/cf_farm.py [--host 127.0.0.1] [--port 8191]

说明：
  - lane 模型：固定工作线程池（TPS_FARM_THREADS，默认 8）= lane 数上限，
    前端池只做 HTTP 解析，解题工作按代理一致性路由到归属 lane 单线程
    （同代理永远同 lane，lane 与代理 1:1 绑定）；每个 lane 线程同一时刻
    只持一个活浏览器会话（建新会话前先关本线程其他会话，Playwright
    同线程单 loop 要求），同一出口复用，cf_clearance 才有效；同代理
    同时只跑一个 fetch（每代理 Semaphore(1)，真单 lane 语义），
    fetch 期间不持任何 entry 锁。
  - 起浏览器全局限并发（TPS_FARM_MAX_SOLVES，默认 4），等待放锁外，
    拿锁二次确认代数；lane 启动期削峰。
  - 每个代理连续失败超 3 次只代数 +1（各线程下次懒建新会话），计数清零；
    旧会话由属主线程下次命中过期代时自己关，绝不跨线程关闭正在 fetch 的会话。
  - 表项 LRU 上限（TPS_FARM_MAX_ENTRIES，默认 128）+ 闲置 TTL 回收
    （TPS_FARM_IDLE_TTL_S，默认 600 秒）；回收只动无 fetch 在跑的表项。
  - GET /healthz 走池外（acceptor 线程直接处理），不被 lane 占满饿死。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Optional
from urllib.parse import urlparse

try:
    from scrapling.fetchers import StealthySession
except ImportError:  # pragma: no cover
    StealthySession = None  # type: ignore[assignment]

log = logging.getLogger("cf_farm")

DEFAULT_PORT = 8191
DEFAULT_MAX_TIMEOUT_MS = 60_000
MIN_TIMEOUT_MS = 5_000
MAX_FAILS_BEFORE_REBUILD = 3  # 连续失败超 3 次重建

_lock = threading.RLock()
# proxy_key -> {"fails", "gen", "seq", "lock", "fetch_sem", "live", "last_used"}
# 锁纪律：_lock 只保护表项字典结构；entry["lock"] 只保护本表项元数据短临界区；
# 绝不持 entry 锁做 fetch / 建浏览器 / 等信号量；绝不持 entry 锁再取 _lock。
_sessions: dict[str, dict[str, Any]] = {}
_MAX_ENTRIES = max(1, int(os.environ.get("TPS_FARM_MAX_ENTRIES", "128")))
_IDLE_TTL_S = max(1.0, float(os.environ.get("TPS_FARM_IDLE_TTL_S", "600")))
_entry_seq = 0  # 表项代际令牌，淘汰重建后递增，防属主线程复用已关旧会话

POOL_SIZE = max(1, int(os.environ.get("TPS_FARM_THREADS", "8")))  # 工作线程数 = lane 数上限
MAX_CONCURRENT_SOLVES = max(1, int(os.environ.get("TPS_FARM_MAX_SOLVES", "4")))  # 同时起浏览器上限
_new_session_sem = threading.BoundedSemaphore(MAX_CONCURRENT_SOLVES)
_TLS = threading.local()  # lane 线程 -> {"sessions": {(proxy_key, seq, gen): session}}

# 一致性路由：proxy_key -> lane 下标（首次命中绑最空的 lane，之后 sticky）。
# 浏览器对象绝不跨线程 + Playwright 同线程单 loop，所以解题工作必须跑在
# 归属 lane 线程上；同 lane 线程同一时刻只持一个活会话（见 _get_session）。
_LANES: list[concurrent.futures.ThreadPoolExecutor] = [
    concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"farm-lane{i}")
    for i in range(POOL_SIZE)
]
_lane_lock = threading.RLock()
_lane_owner: dict[str, int] = {}
_LANE_TLS = threading.local()  # lane 线程标记：{"idx": lane 下标}，防 solve_once 二次转发


def _lane_idx_on_current_thread() -> Optional[int]:
    """本线程若是 lane 工作线程，返回其 lane 下标，否则 None。"""
    return getattr(_LANE_TLS, "idx", None)


def _run_on_lane(idx: int, fn, *args, **kwargs):
    """在归属 lane 线程上执行 fn，并打上 lane 标记（内层 solve_once 见标记即内联）。"""
    prev = getattr(_LANE_TLS, "idx", None)
    _LANE_TLS.idx = idx
    try:
        return fn(*args, **kwargs)
    finally:
        if prev is None:
            try:
                del _LANE_TLS.idx
            except AttributeError:
                pass
        else:
            _LANE_TLS.idx = prev


def _lane_for(proxy_key: str) -> int:
    """取某代理的归属 lane（sticky；新代理落到当前名额最少的 lane）。"""
    with _lane_lock:
        idx = _lane_owner.get(proxy_key)
        if idx is None:
            counts = [0] * len(_LANES)
            for v in _lane_owner.values():
                if 0 <= v < len(counts):
                    counts[v] += 1
            idx = min(range(len(_LANES)), key=lambda i: counts[i])
            _lane_owner[proxy_key] = idx
        return idx


def _proxy_key(proxy_url: str) -> str:
    """会话缓存键：直连用空串，其余按代理 URL 原样区分。"""
    return (proxy_url or "").strip()


def _new_session(proxy_url: str):
    """新建一个 StealthySession（solve_cloudflare=True）。"""
    if StealthySession is None:  # pragma: no cover
        raise RuntimeError("scrapling 未安装，请先 pip install scrapling")
    kwargs: dict[str, Any] = {
        "headless": True,
        "solve_cloudflare": True,
        "timeout": DEFAULT_MAX_TIMEOUT_MS,
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    session = StealthySession(**kwargs)
    start = getattr(session, "start", None)
    if callable(start):
        start()
    return session


def _close_session(session: Any) -> None:
    for name in ("close", "stop", "quit"):
        meth = getattr(session, name, None)
        if callable(meth):
            try:
                meth()
            except Exception:
                pass
            return


def _reap_locked(now: float) -> None:
    """清掉闲置超 TTL 且当前无 fetch 在跑的表项（调用方需持有 _lock）。

    会话 best-effort 关闭（异常吞掉）；属主线程靠 (seq, gen) 失配不再复用，
    下次命中时重建，会话不会毒化。
    """
    for key in [k for k, e in _sessions.items()
                if now - float(e.get("last_used") or 0) >= _IDLE_TTL_S]:
        entry = _sessions.get(key)
        if entry is None:
            continue
        sem = entry.get("fetch_sem")
        if sem is not None and not sem.acquire(blocking=False):
            continue  # 有 fetch 在跑，跳过
        try:
            _sessions.pop(key, None)
        finally:
            if sem is not None:
                try:
                    sem.release()
                except Exception:
                    pass
        for sess in entry.get("live") or []:
            _close_session(sess)


def _evict_oldest_locked() -> bool:
    """淘汰最久未用且无 fetch 在跑的表项（调用方需持有 _lock）。"""
    for key in list(_sessions.keys()):
        entry = _sessions.get(key)
        if entry is None:
            continue
        sem = entry.get("fetch_sem")
        if sem is not None and not sem.acquire(blocking=False):
            continue  # 有 fetch 在跑，换下一个
        try:
            _sessions.pop(key, None)
        finally:
            if sem is not None:
                try:
                    sem.release()
                except Exception:
                    pass
        for sess in entry.get("live") or []:
            _close_session(sess)
        return True
    return False


def _get_entry(proxy_url: str) -> dict[str, Any]:
    """取（或懒建）某代理的计数表项，附带 LRU 上限 + 闲置 TTL 回收。

    只做短临界区元数据操作，绝不在锁内 fetch/建浏览器。
    """
    global _entry_seq
    key = _proxy_key(proxy_url)
    now = time.monotonic()
    with _lock:
        entry = _sessions.get(key)
        if entry is not None:
            with entry["lock"]:
                entry["last_used"] = now
            _sessions.pop(key, None)
            _sessions[key] = entry
            return entry
        _reap_locked(now)
        while len(_sessions) >= _MAX_ENTRIES:
            if not _evict_oldest_locked():
                break
        _entry_seq += 1
        entry = {"fails": 0, "gen": 0, "lock": threading.RLock(),
                 "fetch_sem": threading.Semaphore(1),
                 "live": [], "last_used": now, "seq": _entry_seq}
        _sessions[key] = entry
        return entry


def _get_session(proxy_url: str):
    """取本工作线程的会话：(proxy, seq, 代数) 命中则复用，否则新建。

    同一线程的会话绝不给别的线程用（浏览器对象非线程安全）；
    新建浏览器全局限并发，等待放 entry 锁外，拿锁二次确认代数；
    换代/淘汰后，属主线程下次命中过期键时自己关旧会话并重建。

    Playwright Sync API 约束：同一线程同一时刻只能有一个活着的
    playwright（第二个 sync_playwright().start() 必报
    "Sync API inside the asyncio loop"）。所以本线程建新会话前，
    必须先关掉本线程名下其他所有会话（含同代理旧代、含其他代理），
    保证建的那一刻本线程名下零活会话；调用点在 fetch 之前，
    本线程此时无在途 fetch，被关的都是空闲会话，安全。
    """

    key = _proxy_key(proxy_url)
    entry = _get_entry(proxy_url)
    tls_map = getattr(_TLS, "sessions", None)
    if tls_map is None:
        tls_map = {}
        _TLS.sessions = tls_map
    with entry["lock"]:
        cur = (key, entry["seq"], entry["gen"])
        sess = tls_map.get(cur)
        if sess is not None:
            return sess
    with _new_session_sem:  # 锁外等待，元数据锁不被建浏览器阻塞
        with entry["lock"]:  # 二次确认代数
            cur = (key, entry["seq"], entry["gen"])
            sess = tls_map.get(cur)
            if sess is not None:
                return sess
        # 先关本线程其他会话，再建新会话（同线程绝不双活 playwright）。
        for stale_key in [k for k in tls_map]:
            if stale_key == cur:
                continue
            stale = tls_map.pop(stale_key, None)
            if stale is not None:
                _close_session(stale)  # 关自己的空闲会话，同线程安全
        fresh = _new_session(proxy_url)  # 锁外建浏览器（可能耗时数秒）
        with entry["lock"]:  # 按最新代注册
            cur = (key, entry["seq"], entry["gen"])
            sess = tls_map.get(cur)
            if sess is not None:
                _close_session(fresh)
                return sess
            tls_map[cur] = fresh
            entry["live"].append(fresh)
            for stale_key in [k for k in tls_map
                              if k[0] == key and k != cur]:
                stale = tls_map.pop(stale_key, None)
                if stale is not None and stale is not fresh:
                    _close_session(stale)  # 关自己的旧会话，同线程安全
            return fresh


def _reset_state() -> None:
    """清空会话表与本线程亲和映射（单测/运维用，生产链路不调）。"""
    with _lock:
        _sessions.clear()
    if getattr(_TLS, "sessions", None) is not None:
        _TLS.sessions.clear()


def _record_success(proxy_url: str) -> None:
    with _lock:
        entry = _sessions.get(_proxy_key(proxy_url))
    if entry is not None:
        with entry["lock"]:
            entry["fails"] = 0


def _record_failure(proxy_url: str) -> None:
    """记一次失败；连续失败超 3 次只代数 +1、计数清零，各线程下次懒建新会话。

    不关闭任何会话：旧会话由属主线程下次命中过期代时自己关，避免关掉
    其他线程正在 fetch 的会话造成连锁失败。
    """
    key = _proxy_key(proxy_url)
    with _lock:
        entry = _sessions.get(key)
    if entry is None:
        return
    with entry["lock"]:
        entry["fails"] = int(entry.get("fails") or 0) + 1
        if entry["fails"] <= MAX_FAILS_BEFORE_REBUILD:
            return
        entry["gen"] = int(entry.get("gen") or 0) + 1
        entry["fails"] = 0
        entry["live"] = []


def _extract_cookies(response: Any) -> list[dict[str, str]]:
    """Response.cookies（tuple[dict]/dict/list）收敛成 [{name, value}]。"""
    raw = getattr(response, "cookies", None)
    out: list[dict[str, str]] = []
    if isinstance(raw, dict):
        items: Any = list(raw.items())
        for name, value in items:
            if name:
                out.append({"name": str(name), "value": "" if value is None else str(value)})
        return out
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name", item.get("key", item.get("Name", "")))
                value = item.get("value", item.get("Value", item.get("val", "")))
                if name:
                    out.append({"name": str(name),
                                "value": "" if value is None else str(value)})
            elif isinstance(item, (list, tuple)) and len(item) == 2 and item[0]:
                out.append({"name": str(item[0]),
                            "value": "" if item[1] is None else str(item[1])})
    return out


def _extract_html(response: Any, limit: int = 2000000) -> str:
    """尽力从 Response 拿正文（供调用方“干净页直接采信”），拿不到返回空串。"""
    for attr in ("body", "text", "html", "content"):
        try:
            value = getattr(response, attr, "")
        except Exception:
            continue
        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode("utf-8", errors="replace")
            except Exception:
                continue
        if isinstance(value, str) and value.strip():
            return value[:limit]
    try:
        getter = getattr(response, "get", None)
        if callable(getter):
            value = getter()
            if isinstance(value, str) and value.strip():
                return value[:limit]
    except Exception:
        pass
    return ""


def _extract_ua(response: Any) -> str:
    """从请求头里取实际使用的 User-Agent（大小写不敏感）。"""
    headers = getattr(response, "request_headers", None) or {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "user-agent" and value:
                return str(value)
    return ""


def solve_once(url: str, proxy_url: str = "", timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS) -> tuple:
    """用对应代理的会话解一次 Cloudflare。返回 (cookies, ua, html)。
    无 cookie 且无正文即抛错；干净页（无挑战、无 cookie）带正文返回，调用方采信正文。

    同代理 fetch 用每代理 Semaphore(1) 串行（真单 lane 语义）；entry 锁只做
    元数据短临界区，绝不带进 fetch（≤60s），多 lane 互不阻塞。

    非 lane 线程直调会自动转交归属 lane 再解（冷启动同代理只建一个会话）；
    lane 线程内重入直接内联，避免跨 lane 等待死锁。
    """
    timeout_ms = max(MIN_TIMEOUT_MS, int(timeout_ms or DEFAULT_MAX_TIMEOUT_MS))
    if _lane_idx_on_current_thread() is None:
        lane_idx = _lane_for(_proxy_key(proxy_url))
        fut = _LANES[lane_idx].submit(
            _run_on_lane, lane_idx, solve_once, url, proxy_url, timeout_ms)
        return fut.result()
    session = _get_session(proxy_url)  # 本线程亲和，跨线程不共享
    entry = _get_entry(proxy_url)  # 最新表项，只取元数据
    with entry["lock"]:
        entry["last_used"] = time.monotonic()
    try:
        with entry["fetch_sem"]:
            try:
                response = session.fetch(url, timeout=timeout_ms,
                                         solve_cloudflare=True)
            except Exception as exc:
                _record_failure(proxy_url)
                raise RuntimeError(
                    f"fetch failed: {type(exc).__name__}: {exc}") from exc
    finally:
        with entry["lock"]:
            entry["last_used"] = time.monotonic()
    cookies = _extract_cookies(response)
    html = _extract_html(response)
    if not cookies and not html:
        _record_failure(proxy_url)
        raise RuntimeError("solved but returned no cookies")
    _record_success(proxy_url)
    return cookies, _extract_ua(response), html


def _parse_request(body: Any) -> tuple[str, str, int]:
    """解析 FlareSolverr 兼容请求体，返回 (url, proxy_url, timeout_ms)。"""
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    if body.get("cmd") != "request.get":
        raise ValueError(f"unsupported cmd {body.get('cmd')!r}, expected 'request.get'")
    url = body.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("missing url")
    proxy_raw = body.get("proxy")
    proxy_url = ""
    if isinstance(proxy_raw, dict):
        proxy_url = str(proxy_raw.get("url") or "")
    elif isinstance(proxy_raw, str):
        proxy_url = proxy_raw
    try:
        timeout_ms = int(body.get("maxTimeout")
                         or float(body.get("max_timeout") or 0) * 1000
                         or DEFAULT_MAX_TIMEOUT_MS)
    except (TypeError, ValueError):
        timeout_ms = DEFAULT_MAX_TIMEOUT_MS
    return url.strip(), proxy_url.strip(), timeout_ms


def _proxy_from_headers(headers: Any) -> str:
    """读 Byparr 风格按请求覆盖代理头，拼成全量代理 URL；无头则返回空串。"""
    try:
        get = getattr(headers, "get", None)
        if not callable(get):
            return ""
        server = (get("X-Proxy-Server") or "").strip()
        if not server:
            return ""
        if "://" not in server:
            server = "http://" + server
        user = (get("X-Proxy-Username") or "").strip()
        pwd = (get("X-Proxy-Password") or "").strip()
        if user and pwd:
            scheme, _, rest = server.partition("://")
            return f"{scheme}://{user}:{pwd}@{rest}"
        return server
    except Exception:
        return ""


class FarmHandler(BaseHTTPRequestHandler):
    server_version = "CfFarm/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 走 logging，不刷裸 stdout
        log.info("%s - " + fmt, self.address_string(), *args)

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/healthz":
            data = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send_json(404, {"status": "error", "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/v1":
            self._send_json(404, {"status": "error", "message": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"status": "error", "message": f"invalid JSON: {exc}"})
            return
        try:
            url, proxy_url, timeout_ms = _parse_request(body)
        except ValueError as exc:
            self._send_json(400, {"status": "error", "message": str(exc)})
            return
        if not proxy_url:
            # byparr: 风味代理走请求头（body 无代理字段），有头则用头的。
            proxy_url = _proxy_from_headers(self.headers)
        try:
            lane_idx = _lane_for(_proxy_key(proxy_url))
            lane = _LANES[lane_idx]
            fut = lane.submit(_run_on_lane, lane_idx,
                              solve_once, url, proxy_url, timeout_ms)
            cookies, user_agent, html = fut.result()
        except Exception as exc:
            try:
                shown = "direct" if not proxy_url else urlparse(proxy_url).hostname or "proxy"
            except Exception:
                shown = "proxy"
            log.warning("solve failed url=%s proxy=%s: %s", url, shown, exc)
            self._send_json(200, {"status": "error", "message": str(exc)[:300]})
            return
        solution: dict = {"cookies": cookies, "userAgent": user_agent}
        if html:
            solution["response"] = html
        self._send_json(200, {"status": "ok", "solution": solution})


def _is_healthz_probe(request) -> bool:
    """用 MSG_PEEK 预读请求首行，判断是否为 GET /healthz（不消耗字节）。"""
    try:
        old_timeout = request.gettimeout()
    except Exception:
        old_timeout = None
    try:
        try:
            request.settimeout(1.0)
        except Exception:
            pass
        try:
            peek = request.recv(4096, socket.MSG_PEEK)
        except Exception:
            return False
        if not peek:
            return False
        head = peek.split(b"\r\n", 1)[0].split(b"\n", 1)[0]
        try:
            parts = head.decode("latin-1").split()
        except Exception:
            return False
        return (len(parts) >= 2 and parts[0] == "GET"
                and parts[1].split("?", 1)[0] == "/healthz")
    finally:
        try:
            request.settimeout(old_timeout)
        except Exception:
            pass


class _PoolHTTPServer(http.server.HTTPServer):
    """固定工作线程池的 HTTP 服务：线程数 = lane 数上限，线程常驻，会话亲和。

    ThreadingHTTPServer 每请求起新线程，会话无法跨请求复用（复用则跨线程
    用浏览器，会 explosive）。池化后同一工作线程反复接请求，_get_session
    的 (proxy, seq, 代数) 亲和即等价于“1 线程 + 1 会话 + 1 出口 = 1 lane”。
    GET /healthz 走池外（acceptor 线程直接处理），lane 占满时探针不饿死。
    """

    def __init__(self, server_address, handler_class,
                 pool_size: int = POOL_SIZE) -> None:
        super().__init__(server_address, handler_class)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, pool_size), thread_name_prefix="farm-lane")

    def process_request(self, request, client_address) -> None:
        try:
            if _is_healthz_probe(request):
                self._handle_request(request, client_address)
                return
        except Exception:
            pass
        self._pool.submit(self._handle_request, request, client_address)

    def _handle_request(self, request, client_address) -> None:
        """等价于 ThreadingMixIn.process_request_thread，只是跑在池线程里。"""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self) -> None:
        super().server_close()
        self._pool.shutdown(wait=False)
        for lane in _LANES:
            lane.shutdown(wait=False)


def build_server(host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT) -> _PoolHTTPServer:
    server = _PoolHTTPServer((host, port), FarmHandler)
    return server


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="本地免费 Cloudflare 求解节点（FlareSolverr 兼容 /v1）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if StealthySession is None:  # pragma: no cover
        raise SystemExit("scrapling 未安装，请先 pip install scrapling")
    server = build_server(args.host, args.port)
    log.info("cf_farm listening on http://%s:%d (POST /v1, GET /healthz)",
             args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
