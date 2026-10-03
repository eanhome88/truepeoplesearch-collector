"""穿云控制台代码生成器当前的 v2 Cookie 模式。

对应控制台选择：
- 穿云 v2 (Turnstile WAF)
- 指纹：默认，不发送 x-cb-fp
- 会话分区不启用，不发送 x-cb-part
- 不发送 x-cb-options: force
- 验证 Cookie 由响应 Set-Cookie 交回客户端，下次请求用 Cookie 头带回
- 代理只放在 x-cb-proxy，不作为 HTTP 客户端代理
- 成功看响应头 x-cb-status 是否为 ok

密钥和代理只从参数或环境变量读取。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

API_ORIGIN = "https://api.cloudbypass.com"
DEFAULT_TIMEOUT = 60
CREDIT_KEY = "tps:credit"
SESSION_SEC = 600
STICKY_MINUTES = 30


class GatewayError(Exception):
    """穿云网关失败。kind 为 timeout、rate_limit 或 proxy_fail。"""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class GatewayPage:
    status: int
    body: str
    url: str
    cb_status: str


class CookieStore:
    """按目标主机保存穿云返回的验证 Cookie。"""

    def __init__(self):
        self._by_host: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def header_for(self, host: str) -> str:
        with self._lock:
            pairs = self._by_host.get(host) or {}
            return "; ".join(f"{name}={value}" for name, value in pairs.items())

    def absorb(self, host: str, set_cookies) -> None:
        updates = {}
        for raw in set_cookies or []:
            parsed = _parse_set_cookie(raw)
            if parsed is not None:
                updates[parsed[0]] = parsed[1]
        if not updates:
            return
        with self._lock:
            jar = self._by_host.setdefault(host, {})
            jar.update(updates)

    def clear(self) -> None:
        with self._lock:
            self._by_host.clear()


COOKIES = CookieStore()
_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    try:
        from tps_env import load_project_env
    except ImportError:
        return
    load_project_env(Path(__file__).resolve().parent.parent)


def normalize_proxy(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return "http://" + text
    return text


def resolve_credentials(apikey: Optional[str] = None, proxy: Optional[str] = None):
    _ensure_env()
    if apikey is None:
        key = (os.environ.get("CLOUDBYPASS_APIKEY") or "").strip()
    else:
        key = apikey.strip()
    if proxy is None:
        prx = normalize_proxy(
            os.environ.get("CLOUDBYPASS_PROXY") or os.environ.get("PROXY_TUNNEL")
        )
    else:
        prx = normalize_proxy(proxy)
    return key, prx


def split_target(url: str):
    parsed = urlsplit(url)
    host = parsed.netloc or "www.truepeoplesearch.com"
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    if not path.startswith("/"):
        path = "/" + path
    scheme = parsed.scheme or "https"
    return host, path, scheme


def credit_points(ok: bool, challenge: bool) -> int:
    """成功才扣 1 分。只有真的出现并解掉验证，才再加 2 分。失败不扣基础分。"""
    if not ok:
        return 0
    return 3 if challenge else 1


def saw_challenge(headers) -> bool:
    """穿云没有发起验证时，响应里不会带挑战标记，这次就只记 1 分。"""
    if headers is None:
        return False
    try:
        pairs = list(headers.items())
    except Exception:
        return False
    for key, value in pairs:
        name = str(key).lower()
        text = str(value).strip().lower()
        if name in ("x-cb-challenge", "x-cb-captcha", "x-cb-solved", "x-cb-js") and text in ("1", "true", "yes", "ok", "solved"):
            return True
        if name in ("x-cb-type", "x-cb-action") and any(word in text for word in ("challenge", "turnstile", "captcha")):
            return True
    return False


def sticky_proxy(proxy: str, part: int, minutes: int = STICKY_MINUTES) -> str:
    """同一分区固定一条时效 IP，避免每次换 IP 都重新挑战。"""
    parsed = urlsplit(normalize_proxy(proxy))
    user = parsed.username or ""
    if re.search(r"_s[a-z0-9]+-\d+[smhd]$", user):
        return normalize_proxy(proxy)
    sid = f"{int(part) % 1000:03d}"
    username = f"{user}_s{sid}-{int(minutes)}m"
    password = parsed.password or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{username}:{password}@" if password else f"{username}@"
    return f"http://{auth}{host}{port}"


class SessionBook:
    """分区在 10 分钟内复用时，成功请求只记 1 分。"""

    def __init__(self):
        self._lock = threading.Lock()
        width = int(os.environ.get("CLOUDBYPASS_PARTS", "32") or 32)
        self._size = max(1, min(width, 1000))
        self._free = list(range(self._size))
        self._warm_until = {}

    def checkout(self) -> int:
        with self._lock:
            if not self._free:
                part = self._size % 1000
                self._size += 1
                return part
            return self._free.pop()

    def checkin(self, part: int) -> None:
        with self._lock:
            if part not in self._free:
                self._free.append(part)

    def take(self, part: int, ok: bool, now: Optional[float] = None):
        moment = time.monotonic() if now is None else now
        with self._lock:
            challenge = bool(ok) and self._warm_until.get(part, 0) <= moment
            if ok:
                self._warm_until[part] = moment + SESSION_SEC
        return credit_points(ok, challenge), challenge


SESSIONS = SessionBook()


def build_headers(
    host: str,
    apikey: str,
    proxy: str,
    scheme: str,
    cookie_header: str,
    timeout: int,
    part: Optional[int] = None,
):
    headers = {
        "x-cb-apikey": apikey,
        "x-cb-host": host,
        "x-cb-version": "2",
        "x-cb-proxy": proxy,
        "x-cb-protocol": scheme,
        "x-cb-timeout": str(_clamp_timeout(timeout)),
    }
    if part is None:
        headers["x-cb-options"] = "full-cookie"
        if cookie_header:
            headers["Cookie"] = cookie_header
    else:
        headers["x-cb-part"] = str(int(part) % 1000)
    return headers


def _clamp_timeout(timeout: int) -> int:
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(5, min(value, 360))


def _parse_set_cookie(raw: str):
    if not raw:
        return None
    pair = raw.split(";", 1)[0].strip()
    if "=" not in pair:
        return None
    name, value = pair.split("=", 1)
    name = name.strip()
    if not name or name.lower() in {"path", "domain", "expires", "max-age", "samesite"}:
        return None
    return name, value.strip()


def _set_cookies_of(response) -> list:
    getter = getattr(response.headers, "get_list", None)
    if callable(getter):
        values = getter("set-cookie")
        if values:
            return list(values)
    single = response.headers.get("set-cookie")
    return [single] if single else []


def _page_from_response(response, url: str):
    status = int(response.status_code)
    body = response.text or ""
    cb_status = (response.headers.get("x-cb-status") or "").strip().lower()
    if "INSUFFICIENT_BALANCE" in body:
        raise GatewayError(f"Cloudbypass 积分不足 for {url}", "balance")
    if status == 429 or '"TOO_MANY_REQUESTS"' in body or "rate limited" in body.lower():
        raise GatewayError(f"Cloudbypass 429 for {url}", "rate_limit")
    if cb_status != "ok":
        return None
    return GatewayPage(status=status, body=body, url=url, cb_status=cb_status)


def record_credit(points: int, challenge: bool, ok: bool) -> None:
    try:
        pipe = connect_redis().pipeline(transaction=False)
        pipe.hincrby(CREDIT_KEY, "points", int(points))
        if ok and challenge:
            pipe.hincrby(CREDIT_KEY, "challenge", 1)
        elif ok:
            pipe.hincrby(CREDIT_KEY, "success", 1)
        else:
            pipe.hincrby(CREDIT_KEY, "fail", 1)
        pipe.execute()
    except Exception:
        return


def _raise_transport(exc: Exception, url: str) -> None:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        raise GatewayError(f"Cloudbypass timeout for {url}", "timeout") from exc
    raise GatewayError(f"Cloudbypass fetch failed: {exc}", "proxy_fail") from exc


async def fetch_async(
    url: str,
    apikey: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    client=None,
    cookies: Optional[CookieStore] = None,
    session: bool = False,
    sessions: Optional[SessionBook] = None,
) -> Optional[GatewayPage]:
    key, prx = resolve_credentials(apikey, proxy)
    if not key or not prx:
        return None
    try:
        import httpx
    except ImportError:
        return None

    del sessions
    jar = cookies if cookies is not None else COOKIES
    host, path, scheme = split_target(url)
    api_url = API_ORIGIN + path
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=_clamp_timeout(timeout) + 10)
    try:
        page = None
        for attempt in range(max(1, int(max_retries))):
            headers = build_headers(
                host, key, prx, scheme, jar.header_for(host), timeout,
            )
            try:
                response = await client.get(api_url, headers=headers)
            except Exception as exc:
                _raise_transport(exc, url)
            jar.absorb(host, _set_cookies_of(response))
            challenged = saw_challenge(response.headers)
            try:
                page = _page_from_response(response, url)
            except GatewayError:
                if session:
                    record_credit(0, False, False)
                raise
            if page is not None or attempt >= max_retries - 1:
                if session:
                    record_credit(credit_points(page is not None, challenged), challenged and page is not None, page is not None)
                return page
            await asyncio.sleep(1.0)
        return page
    finally:
        if own_client:
            await client.aclose()


def fetch_sync(
    url: str,
    apikey: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    client=None,
    cookies: Optional[CookieStore] = None,
) -> Optional[GatewayPage]:
    key, prx = resolve_credentials(apikey, proxy)
    if not key or not prx:
        return None
    try:
        import httpx
    except ImportError:
        return None

    jar = cookies if cookies is not None else COOKIES
    host, path, scheme = split_target(url)
    api_url = API_ORIGIN + path
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=_clamp_timeout(timeout) + 10)
    try:
        page = None
        for attempt in range(max(1, int(max_retries))):
            headers = build_headers(host, key, prx, scheme, jar.header_for(host), timeout)
            try:
                response = client.get(api_url, headers=headers)
            except Exception as exc:
                _raise_transport(exc, url)
            jar.absorb(host, _set_cookies_of(response))
            page = _page_from_response(response, url)
            if page is not None or attempt >= max_retries - 1:
                return page
            time.sleep(1.0)
        return page
    finally:
        if own_client:
            client.close()
