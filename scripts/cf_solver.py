#!/usr/bin/env python3
"""
自研过 Cloudflare 求解器接入层。

TPS_OWN_CF=1 时 worker 不再开浏览器解 Cloudflare，而是向 TPS_CF_SOLVER 指定的求解器
要一份放行凭证（cookies + User-Agent），然后用 curl_cffi 模拟同版本 Chrome 的 TLS 指纹，
在同一条代理出口上发协议请求。一份凭证复用到被拒或会话回收为止。

TPS_CF_SOLVER 三种写法：
  package.module:callable      Python 函数，同步/异步都行
  http://127.0.0.1:9000/solve  HTTP 服务，POST JSON
  cmd:D:\\tools\\solver.exe      命令行程序，stdin 收 JSON，stdout 回 JSON
免费自建节点（本机跑，不花钱）：
  flaresolverr:http://127.0.0.1:8191/v1   FlareSolverr 兼容 API（Byparr v2 同接口）
  byparr:http://127.0.0.1:8191/v1        同上，别名
  这两种把 FlareSolverr 的 solution{cookies,userAgent,response} 转成统一凭证，
  直接插进 TPS_OWN_CF=1 的协议会话。Byparr(Camoufox 内核)是 2026 年免费档里
  对 Turnstile/Managed Challenge 成功率最高的，FlareSolverr 本体已过时别用。
  代理透传规则（cf_clearance 绑 IP，解题出口必须与使用出口一致）：
  byparr 风味走 X-Proxy-Server/Username/Password 请求头（body 无代理字段）；
  flaresolverr 风味带认证的代理走 sessions.create 建会话复用，不支持 sessions
  的节点（如自建 cf_farm）自动降级无状态单发（此时发全量代理，含认证）。
  cf_farm 同时认 X-Proxy-Server/Username/Password 请求头（byparr 风味）。成功必须带 cf_clearance。

三种写法收到的参数一致：
  {"url": "...", "proxy": "http://user:pass@host:port" 或 null,
   "user_agent": "..." 或 null, "timeout": 45}
  proxy 是本组绑定的出口。cf_clearance 绑 IP，求解器必须走同一出口。
  user_agent 为空表示首解，求解器自选 UA；非空表示重解，请沿用这个 UA。

返回（字段名宽松，任一写法都认）：
  {"cookies": {"cf_clearance": "..."} 或 [{"name": ..., "value": ...}] 或 "k=v; k2=v2",
   "user_agent" / "ua" / "userAgent": "...",
   "html": "可选。求解器已经拿到该 url 正文就直接给，省一次请求",
   "ttl": 1500}
  可以整体包在 {"data": {...}} / {"result": {...}} 里。
  {"ok": false, "error": "..."} 或 {"success": false} 视为失败。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

DEFAULT_SOLVER_TIMEOUT_SEC = 45.0
DEFAULT_TTL_SEC = 1500.0
# FlareSolverr /v1 单次求解上限：超过 120s 的 maxTimeout 直接钳掉，
# 否则 socket 先超时、服务端浏览器还在解，等于泄漏一次解题槽位。
_FLARE_MAX_TIMEOUT_MS = 120_000
_FLARE_MIN_TIMEOUT_MS = 1_000
_FLARE_SESSION_TTL_MIN = 30
_CLEARANCE_COOKIE = "cf_clearance"
DEFAULT_IMPERSONATE = "chrome124"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_WRAPPER_KEYS = ("data", "result", "solution")
_UA_KEYS = ("user_agent", "ua", "userAgent", "user-agent", "useragent")
_COOKIE_KEYS = ("cookies", "cookie", "Cookie")
_HTML_KEYS = ("html", "body", "content")


class CfSolverError(RuntimeError):
    """求解器不可用、超时或返回了没法用的东西。"""


@dataclass
class CfSolution:
    cookies: dict = field(default_factory=dict)
    user_agent: str = ""
    html: Optional[str] = None
    ttl: float = DEFAULT_TTL_SEC
    solved_at: float = field(default_factory=time.time)

    @property
    def has_clearance(self) -> bool:
        return bool(self.cookies)

    @property
    def expires_at(self) -> float:
        try:
            ttl = float(self.ttl)
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL_SEC
        if ttl <= 0:
            ttl = DEFAULT_TTL_SEC
        return float(self.solved_at) + ttl

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if k)


def _cookies_dict(value: Any) -> dict:
    out: dict = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if k is None or v is None:
                continue
            k = str(k).strip()
            if k:
                out[k] = str(v)
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("key")
                val = item.get("value")
                if name and val is not None:
                    out[str(name).strip()] = str(val)
            elif isinstance(item, str):
                out.update(_cookies_dict(item))
        return out
    if isinstance(value, str):
        for piece in value.split(";"):
            if "=" not in piece:
                continue
            k, _, v = piece.partition("=")
            k = k.strip()
            if k:
                out[k] = v.strip()
        return out
    return out


def _first(body: dict, keys: tuple) -> Any:
    for key in keys:
        if key in body and body[key] not in (None, ""):
            return body[key]
    return None


def _split_proxy_auth(proxy: Optional[str]) -> tuple:
    """拆代理 userinfo：返回 (去认证裸url, 用户名, 密码)。无认证则后两项为空串。"""
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
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    try:
        user = unquote(parts.username or "")
        pwd = unquote(parts.password or "")
    except Exception:
        user, pwd = "", ""
    return f"{parts.scheme}://{host}", user, pwd


def _same_site(url_a: str, url_b: str) -> bool:
    """解后落点是否同站（允许 www/apex 互跳）。解析失败不挡路，返回 True。"""
    try:
        ha = (urlparse(url_a).hostname or "").lower()
        hb = (urlparse(url_b).hostname or "").lower()
    except Exception:
        return True
    if not ha or not hb:
        return True
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def _looks_like_dead_session(reason: str) -> bool:
    low = (reason or "").lower()
    return "session" in low and any(k in low for k in ("invalid", "expired", "not found", "unknown", "does not exist"))


def _html_is_challenged(html: str) -> bool:
    """求解器带回的正文是否还是挑战页。优先复用主仓判定，拿不到才用内建标记。"""
    text = html or ""
    if len(text.strip()) < 40:
        return True
    try:
        from scrape_to_tidb import is_challenge_html
        return bool(is_challenge_html(text))
    except Exception:
        pass
    low = text.lower()
    return any(m in low for m in (
        "internalcaptcha", "just a moment", "cf-challenge", "cf-turnstile",
        "attention required", "checking your browser", "access denied"))


def normalize_solution(raw: Any) -> CfSolution:
    """把求解器五花八门的返回收敛成 CfSolution。没有 cookies 也没有 html 就是失败。"""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise CfSolverError("solver returned empty output")
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise CfSolverError(f"solver returned non-JSON output: {text[:120]!r}") from exc
    if isinstance(raw, CfSolution):
        return raw
    if not isinstance(raw, dict):
        raise CfSolverError(f"solver returned {type(raw).__name__}, expected object")

    if raw.get("ok") is False or raw.get("success") is False:
        reason = raw.get("error") or raw.get("message") or raw.get("msg") or "solver reported failure"
        raise CfSolverError(str(reason)[:300])

    body = raw
    for key in _WRAPPER_KEYS:
        inner = raw.get(key)
        if isinstance(inner, dict):
            body = inner
            break

    cookies = _cookies_dict(_first(body, _COOKIE_KEYS))
    ua = _first(body, _UA_KEYS)
    ua = str(ua).strip() if ua is not None else ""
    html = _first(body, _HTML_KEYS)
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", errors="replace")
    if not isinstance(html, str) or not html.strip():
        html = None
    try:
        ttl = float(body.get("ttl") or DEFAULT_TTL_SEC)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_SEC
    if ttl <= 0:
        ttl = DEFAULT_TTL_SEC

    if not cookies and html is None:
        raise CfSolverError("solver returned neither cookies nor html")
    return CfSolution(cookies=cookies, user_agent=ua, html=html, ttl=ttl)


_CHROME_TARGET_RE = re.compile(r"^chrome(\d+)[a-z]?$")


def _supported_chrome_targets() -> dict:
    """curl_cffi 当前版本支持的 chrome 伪装目标：{主版本: 目标名}。拿不到就用静态表。"""
    names: list = []
    try:
        import typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral  # type: ignore

        names = [str(n) for n in typing.get_args(BrowserTypeLiteral)]
    except Exception:
        names = [
            "chrome99", "chrome100", "chrome101", "chrome104", "chrome107", "chrome110",
            "chrome116", "chrome119", "chrome120", "chrome123", "chrome124", "chrome131",
            "chrome133a", "chrome136",
        ]
    out: dict = {}
    for name in names:
        m = _CHROME_TARGET_RE.match(name)
        if m:
            out[int(m.group(1))] = name
    return out


def impersonate_for_ua(user_agent: Optional[str]) -> str:
    """按 UA 里的 Chrome 主版本挑最接近且不高于它的 curl_cffi 伪装目标。
    TLS 指纹和 UA 版本对不上会被 Cloudflare 的一致性检查盯上。TPS_IMPERSONATE 可强制覆盖。"""
    forced = (os.environ.get("TPS_IMPERSONATE") or "").strip()
    if forced:
        return forced
    targets = _supported_chrome_targets()
    if not targets:
        return DEFAULT_IMPERSONATE
    m = re.search(r"Chrome/(\d+)", user_agent or "")
    if not m:
        return targets.get(124) or DEFAULT_IMPERSONATE
    major = int(m.group(1))
    candidates = [v for v in targets if v <= major]
    if not candidates:
        return targets[min(targets)]
    return targets[max(candidates)]


class CfSolver:
    """统一调用面。kind ∈ {python, http, cmd, flaresolverr}。solve() 永远是协程，不阻塞事件循环。"""

    def __init__(self, spec: str, timeout: float = DEFAULT_SOLVER_TIMEOUT_SEC):
        self.spec = (spec or "").strip()
        if not self.spec:
            raise CfSolverError("TPS_CF_SOLVER is empty")
        self.timeout = max(5.0, float(timeout))
        self.kind, self._target = self._parse(self.spec)
        # byparr: 与 flaresolverr: 同走 /v1，但代理必须走 X-Proxy-* 请求头
        #（body 里无代理字段，会被静默忽略），且不支持 sessions.*。
        self._flavor = "byparr" if self.spec.lower().startswith("byparr:") else "flaresolverr"
        # FlareSolverr 会话复用：proxy -> session id。锁保护，to_thread 下安全。
        self._flare_lock = threading.Lock()
        self._flare_sessions: dict = {}
        # sessions.create 是否可用：None 未探明，False 表示对端不支持（如自建 cf_farm），
        # 之后直接走无状态 request.get，不再试探。
        self._flare_session_ok: Optional[bool] = None

    @staticmethod
    def _parse(spec: str) -> tuple:
        low = spec.lower()
        for prefix in ("flaresolverr:", "flaresolver:", "byparr:"):
            if low.startswith(prefix):
                base = spec[len(prefix):].strip().rstrip("/")
                if not base.lower().startswith(("http://", "https://")):
                    raise CfSolverError(f"TPS_CF_SOLVER {prefix} needs an http(s) URL, got {base!r}")
                return "flaresolverr", base
        if low.startswith(("http://", "https://")):
            return "http", spec
        if low.startswith("cmd:"):
            cmd = spec[4:].strip()
            if not cmd:
                raise CfSolverError("TPS_CF_SOLVER cmd: has no command")
            return "cmd", cmd
        module_name, sep, attr = spec.rpartition(":")
        if not sep or not module_name or not attr:
            raise CfSolverError(
                "TPS_CF_SOLVER must be module:callable, http(s)://..., cmd:<command>, "
                "flaresolverr:http(s)://host/v1 or byparr:http(s)://host/v1"
            )
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise CfSolverError(f"cannot import solver module {module_name!r}: {exc}") from exc
        fn = getattr(module, attr, None)
        if not callable(fn):
            raise CfSolverError(f"{spec!r} is not callable")
        return "python", fn

    def describe(self) -> str:
        if self.kind in ("http", "flaresolverr"):
            parts = urlparse(self._target)
            return f"{self.kind} {parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}{parts.path}"
        if self.kind == "cmd":
            return f"cmd {self._target.split()[0]}"
        fn: Callable = self._target
        return f"python {getattr(fn, '__module__', '?')}:{getattr(fn, '__name__', '?')}"

    async def solve(
        self,
        url: str,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CfSolution:
        to = float(timeout or self.timeout)
        payload = {
            "url": url,
            "proxy": proxy or None,
            "user_agent": user_agent or None,
            "timeout": to,
        }
        try:
            if self.kind == "python":
                raw = await asyncio.wait_for(self._call_python(payload), to + 5.0)
            elif self.kind == "http":
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self._post_json, payload, to), to + 10.0
                )
            elif self.kind == "flaresolverr":
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self._solve_flaresolverr, payload, to), to + 10.0
                )
            else:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self._run_cmd, payload, to), to + 10.0
                )
        except CfSolverError:
            raise
        except asyncio.TimeoutError as exc:
            raise CfSolverError(f"solver timed out after {to:.0f}s") from exc
        except Exception as exc:
            raise CfSolverError(f"solver failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
        return normalize_solution(raw)

    async def _call_python(self, payload: dict) -> Any:
        fn: Callable = self._target
        if inspect.iscoroutinefunction(fn):
            return await fn(**payload)
        result = await asyncio.to_thread(fn, **payload)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _post_json(self, payload: dict, timeout: float) -> Any:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._target,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout + 5.0) as resp:
                body = resp.read()
                status = int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise CfSolverError(f"solver HTTP {exc.code}: {detail}") from exc
        if status >= 400:
            raise CfSolverError(f"solver HTTP {status}")
        return body

    def _flare_post(self, body: dict, headers: Optional[dict], timeout_s: float) -> dict:
        """向 /v1 发一个 JSON POST，返回解析后的 dict。HTTP 错误时把对端 message 拼进异常。"""
        data = json.dumps(body).encode("utf-8")
        heads = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            heads.update(headers)
        req = urllib.request.Request(
            self._target,
            data=data,
            method="POST",
            headers=heads,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            message = detail
            try:
                parsed = json.loads(detail)
                if isinstance(parsed, dict):
                    message = parsed.get("message") or parsed.get("error") or detail
            except Exception:
                pass
            raise CfSolverError(f"solver HTTP {exc.code}: {str(message)[:200]}") from exc
        try:
            doc = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise CfSolverError("solver returned non-JSON output") from exc
        if not isinstance(doc, dict):
            raise CfSolverError("solver returned non-object output")
        return doc

    def _flare_ensure_session(self, proxy_key: str, bare: str, user: str, pwd: str,
                              timeout_s: float) -> str:
        """取（或懒建）某代理的 FlareSolverr 会话。建会话失败直接抛，调用方负责降级。"""
        with self._flare_lock:
            sid = self._flare_sessions.get(proxy_key)
            if sid:
                return sid
        fresh = uuid.uuid4().hex
        doc = self._flare_post(
            {"cmd": "sessions.create", "session": fresh,
             "proxy": {"url": bare, "username": user, "password": pwd},
             "session_ttl_minutes": _FLARE_SESSION_TTL_MIN},
            None, timeout_s,
        )
        if doc.get("status") != "ok":
            reason = str(doc.get("message") or doc.get("error") or "")[:200]
            raise CfSolverError(f"sessions.create failed: {reason}")
        with self._flare_lock:
            self._flare_sessions[proxy_key] = fresh
        return fresh

    def _solve_flaresolverr(self, payload: dict, timeout: float) -> Any:
        """FlareSolverr 兼容 API（Byparr v2 同接口）-> 统一凭证。
        POST {cmd: request.get, url, proxy/session, maxTimeout}，取 solution{cookies,userAgent,response}。
        - 认证代理：FlareSolverr 的 request.get 不认 userinfo，必须走 sessions.create 建会话复用；
          建会话失败（如自建 cf_farm 不支持 sessions.*）则自动降级无状态单发。
        - byparr: 风味：body 无代理字段，代理走 X-Proxy-Server/Username/Password 请求头；
          maxTimeout 与 max_timeout 双发（毫秒/秒各一份，老版本只认其一也不怕）。
        - 成功判定：必须带 cf_clearance（NID 之类路人 cookie 不算数），落点必须同站，
          solution.response 有正文就带回，省一次回源。"""
        raw_url = payload.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
        if not url:
            raise CfSolverError("solver payload has no url")
        try:
            to = float(payload.get("timeout") or timeout)
        except (TypeError, ValueError):
            to = float(timeout)
        max_ms = max(_FLARE_MIN_TIMEOUT_MS, min(int(to * 1000), _FLARE_MAX_TIMEOUT_MS))
        sock_to = max_ms / 1000.0 + 10.0
        proxy = payload.get("proxy") or ""
        bare, user, pwd = _split_proxy_auth(proxy) if proxy else ("", "", "")

        if self._flavor == "byparr":
            body = {"cmd": "request.get", "url": url,
                    "maxTimeout": max_ms, "max_timeout": (max_ms + 999) // 1000}
            headers: dict = {}
            if proxy:
                if bare:
                    headers["X-Proxy-Server"] = bare
                if user:
                    headers["X-Proxy-Username"] = user
                if pwd:
                    headers["X-Proxy-Password"] = pwd
            doc = self._flare_post(body, headers or None, sock_to)
        else:
            session_id = ""
            if proxy and (user or pwd) and self._flare_session_ok is not False:
                try:
                    session_id = self._flare_ensure_session(
                        proxy, bare, user, pwd, min(sock_to, 30.0))
                    self._flare_session_ok = True
                except CfSolverError:
                    # 对端不支持 sessions.*（如自建 cf_farm）：记死，之后走无状态。
                    self._flare_session_ok = False
                    session_id = ""
            body = {"cmd": "request.get", "url": url, "maxTimeout": max_ms}
            if proxy:
                if session_id:
                    # 会话模式：认证走会话里的凭据，body 只带裸出口。
                    body["proxy"] = {"url": bare or proxy}
                else:
                    # 无状态回退必须发全量代理（含 userinfo）：自建 cf_farm 靠它做认证；
                    # 原生 FlareSolverr 本来就忽略 userinfo，发全量与发裸 url 等价，无损失。
                    body["proxy"] = {"url": proxy}
            if session_id:
                body["session"] = session_id
            try:
                doc = self._flare_post(body, None, sock_to)
            except CfSolverError as exc:
                if not (session_id and _looks_like_dead_session(str(exc))):
                    raise
                with self._flare_lock:
                    self._flare_sessions.pop(proxy, None)
                body.pop("session", None)
                doc = self._flare_post(body, None, sock_to)

        if doc.get("status") != "ok":
            reason = str(doc.get("message") or doc.get("error") or "")[:200]
            raise CfSolverError(f"flaresolverr did not solve: {reason}")
        sol = doc.get("solution") or {}
        if not isinstance(sol, dict):
            raise CfSolverError("flaresolverr solution is not an object")
        cookies = {}
        for item in sol.get("cookies") or []:
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                cookies[str(item["name"])] = str(item["value"])
        ua = sol.get("userAgent") or sol.get("user_agent") or ""
        sol_url = sol.get("url") or ""
        if sol_url and not _same_site(url, str(sol_url)):
            raise CfSolverError(f"flaresolverr landed off-site: {str(sol_url)[:120]}")
        out = {"cookies": cookies, "user_agent": str(ua or "")}
        html = sol.get("response") or sol.get("html") or ""
        if isinstance(html, str) and html.strip():
            out["html"] = html
        if _CLEARANCE_COOKIE not in cookies:
            # 无 clearance 但带回干净正文 = 该出口根本没被挑战，直接采信正文。
            html_back = out.get("html") or ""
            if html_back and not _html_is_challenged(html_back):
                return out
            seen = ",".join(sorted(cookies)) or "none"
            raise CfSolverError(
                f"flaresolverr solved but no {_CLEARANCE_COOKIE} (cookies: {seen})")
        return out

    def _run_cmd(self, payload: dict, timeout: float) -> Any:
        args = shlex.split(self._target, posix=(os.name != "nt"))
        if os.name == "nt":
            args = [a.strip('"') for a in args]
        try:
            proc = subprocess.run(
                args,
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True,
                timeout=timeout + 5.0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CfSolverError(f"solver command timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise CfSolverError(f"cannot run solver command: {exc}") from exc
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
            raise CfSolverError(f"solver command exit {proc.returncode}: {err}")
        return proc.stdout


def load_cf_solver_from_env() -> Optional[CfSolver]:
    """TPS_CF_SOLVER 没配返回 None；配了但加载失败直接抛，让 worker 启动期就死，不要跑起来才发现。"""
    spec = (os.environ.get("TPS_CF_SOLVER") or "").strip()
    if not spec:
        return None
    try:
        timeout = float(os.environ.get("TPS_CF_SOLVER_TIMEOUT") or DEFAULT_SOLVER_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = DEFAULT_SOLVER_TIMEOUT_SEC
    return CfSolver(spec, timeout=timeout)


def require_cf_solver() -> CfSolver:
    solver = load_cf_solver_from_env()
    if solver is None:
        raise CfSolverError(
            "TPS_OWN_CF=1 but TPS_CF_SOLVER is not set. Without a solver nothing passes Cloudflare. "
            "Set TPS_CF_SOLVER=module:callable | http://host:port/path | cmd:<command> | "
            "flaresolverr:http://127.0.0.1:8191/v1 | byparr:http://127.0.0.1:8191/v1, "
            "or set TPS_OWN_CF=0 to use the built-in browser solver."
        )
    return solver


def sid_for_proxy(proxy: Optional[str]) -> str:
    """出口标识：粘性网关取用户名里的 session 段，否则取代理地址的短哈希。直连返回空。"""
    raw = (proxy or "").strip()
    if not raw:
        return ""
    parts = urlparse(raw if "://" in raw else "http://" + raw)
    user = parts.username or ""
    m = re.search(r"(?i)-sid-([A-Za-z0-9]+)", user) or re.search(r"(?i)-session[_-]([A-Za-z0-9]+)", user)
    if m:
        return m.group(1)[:12]
    import hashlib

    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def publish_warmed(url: str, solution: CfSolution, sid: str = "") -> None:
    """把凭证按 protocol_fetcher 读的键发布到 Redis（unblocker:warmed:{host}[:{sid}]），
    协议 fleet 可以直接拿去用。Redis 不在就静默跳过，这不是主路径。"""
    if not solution.has_clearance:
        return
    try:
        import redis  # type: ignore

        host = urlparse(url).netloc.lower() or "www.truepeoplesearch.com"
        payload = json.dumps({
            "cookies": dict(solution.cookies),
            "user_agent": solution.user_agent or "",
            "sid": sid or "",
            "ts": time.time(),
        }, ensure_ascii=False)
        r = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=0.4,
            socket_timeout=0.8,
        )
        ttl = int(max(60.0, min(solution.ttl, 86400.0)))
        r.set(f"unblocker:warmed:{host}", payload, ex=ttl)
        if sid:
            r.set(f"unblocker:warmed:{host}:{sid.strip()[:12]}", payload, ex=ttl)
    except Exception:
        pass


# ---- 站内 captcha 快路径：分型先行，重解只给 CF ----
# 背景：第 3 页转站内 InternalCaptcha 时 classify 已判 site_captcha，
# 此时再调 solver 重解 CF 纯属浪费（28.4s 级解题槽位）。只有
# turnstile / managed 才是 CF 挑战、重解可能有用；其余一律换出口。
# 本节只加纯函数，不动 flaresolverr:/byparr: 成功判定、无 sessions 等契约。
CF_RESOLVE_KINDS = frozenset({"turnstile", "managed"})
FASTPATH_SOLVER_ACTION = "solver"
FASTPATH_ROTATE_ACTION = "rotate_proxy"

# cf_challenge 缺失时的保底标记（与 cf_challenge.py 同源，顺序一致：
# site 优先于 turnstile，turnstile 优先于 managed）。
_FASTPATH_SITE_MARKERS = ("internalcaptcha",)
_FASTPATH_TURNSTILE_MARKERS = (
    "cf-turnstile", "turnstile", "challenges.cloudflare.com",
    "cf_chl_", "__cf_chl_",
)
_FASTPATH_MANAGED_MARKERS = (
    "just a moment", "cf-challenge", "checking your browser",
    "cf-browser-verification", "challenge-platform",
)


def needs_cf_resolve(kind: Any) -> bool:
    """该分型是否值得调 solver 重解。仅 turnstile/managed 返回 True。

    大小写/前后空格不敏感；None、空串、未知分型一律 False（= 换出口）。
    """
    if not isinstance(kind, str):
        return False
    return kind.strip().lower() in CF_RESOLVE_KINDS


def fastpath_action(kind: Any) -> str:
    """分型 -> 快路径动作：值得重解返回 "solver"，否则返回 "rotate_proxy"（换出口信号）。"""
    return FASTPATH_SOLVER_ACTION if needs_cf_resolve(kind) else FASTPATH_ROTATE_ACTION


def _fastpath_fallback_kind(url: str = "", html: str = "") -> str:
    blob = f"{url or ''}\n{html or ''}".lower()
    if any(m in blob for m in _FASTPATH_SITE_MARKERS):
        return "site_captcha"
    if any(m in blob for m in _FASTPATH_TURNSTILE_MARKERS):
        return "turnstile"
    if any(m in blob for m in _FASTPATH_MANAGED_MARKERS):
        return "managed"
    return "unknown_fail"


def classify_for_fastpath(
    url: str = "",
    html: str = "",
    status: Any = None,
    exc: Optional[BaseException] = None,
) -> str:
    """快路径分型：优先复用 cf_challenge.classify，拿不到时用内建保底标记。

    纯函数，不出网、不起浏览器；cf_challenge 缺失/抛异常时不炸，返回保底分型。
    """
    try:
        from cf_challenge import classify as _classify
        return str(_classify(url=url, html=html, status=status, exc=exc))
    except Exception:
        pass
    return _fastpath_fallback_kind(url, html)


def should_resolve_challenge(
    url: str = "",
    html: str = "",
    status: Any = None,
    exc: Optional[BaseException] = None,
    kind: Any = None,
) -> bool:
    """快路径总闸：见 site_captcha 直接 False（换出口，不触发 resolve 重解）。

    kind 显式传入时直接用它判定（省一次 classify）；为空时先分型再判定。
    仅 turnstile/managed 返回 True。
    """
    if isinstance(kind, str) and kind.strip():
        return needs_cf_resolve(kind)
    return needs_cf_resolve(classify_for_fastpath(url=url, html=html, status=status, exc=exc))
