#!/usr/bin/env python3
"""
高性能 IP 代理池管理器 (Proxy Pool Manager)

支持三种接入模式：
1. 隧道代理 / 轮换网关 (Tunnel Proxy)：
   单个固定 host:port:user:pass，服务商每次请求自动轮换出口 IP。最推荐、最省资源。
2. 本地文件列表 (Proxy File)：
   从本地 txt 文件读取代理列表，支持轮换、健康打分与临时屏蔽。
3. API 动态提取 (API Fetch)：
   定时调用代理服务商 API 提取新 IP 补充到内存池中。

支持粘性会话 (Sticky Session)：同一个 IP 可复用 N 次请求以减少握手开销，随后切换。

浏览器抓取用 StickyLanes：一条 IP 固定给一个浏览器，直到 HTTP 429，
再冷却这条 IP，换到另一条已经休息好的。不要每条请求换出口。
"""

from __future__ import annotations

import asyncio
import csv
import json
import urllib.request
from contextlib import contextmanager
import os
import random
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import quote, urlparse, urlunparse

# 同一条出口 429 之后，日志里下一次成功隔了大约 70 分钟。
IP_REST_SEC = 70 * 60

try:
    import httpx
except ImportError:
    httpx = None


@dataclass
class ProxyNode:
    url: str                          # 格式: http://user:pass@host:port 或 socks5://...
    fail_count: int = 0
    success_count: int = 0
    cooldown_until: float = 0.0       # 封禁冷却时间戳
    in_use_count: int = 0
    used_times: int = 0               # 已用于多少次请求 (用于粘性会话)

    @property
    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until and self.fail_count < 5


class ProxyManager:
    """统一的代理池调度器"""

    def __init__(
        self,
        tunnel: Optional[str] = None,
        proxy_file: Optional[str] = None,
        api_url: Optional[str] = None,
        sticky_requests: int = 20,       # 单个 IP 粘性复用次数 (0 表示每次换)
        cooldown_sec: float = 60.0,      # 失败冷却时间
        api_refresh_sec: float = 30.0,   # API 刷新间隔
    ):
        self.tunnel = self._normalize_proxy(tunnel) if tunnel else None
        self.proxy_file = proxy_file
        self.api_url = api_url
        self.sticky_requests = sticky_requests
        self.cooldown_sec = cooldown_sec
        self.api_refresh_sec = api_refresh_sec

        self._nodes: List[ProxyNode] = []
        self._index: int = 0
        self._lock = asyncio.Lock()
        self._api_task: Optional[asyncio.Task] = None

        if self.tunnel:
            print(f"[PROXY] 使用隧道轮换代理: {self._mask_proxy(self.tunnel)}")
        elif self.proxy_file:
            self.load_from_file(self.proxy_file)
            print(f"[PROXY] 从文件加载代理: {len(self._nodes)} 个可用")
        elif self.api_url:
            print("[PROXY] 使用 API 动态提取")
        else:
            print("[PROXY] 未指定代理，默认使用直连 (Direct)")

    @staticmethod
    def _normalize_proxy(p: str) -> str:
        p = p.strip()
        if not p:
            return ""
        if not (
            p.startswith("http://")
            or p.startswith("https://")
            or p.startswith("socks5://")
            or p.startswith("socks5h://")
        ):
            p = f"http://{p}"
        return p

    @staticmethod
    def _mask_proxy(p: str) -> str:
        """脱敏显示代理，隐藏密码"""
        try:
            parsed = urlparse(p)
            if "@" in parsed.netloc:
                hostname = parsed.netloc.rsplit("@", 1)[1]
                netloc = f"{parsed.username or 'user'}:****@{hostname}"
                parsed = parsed._replace(netloc=netloc)
            if parsed.query or parsed.fragment:
                parsed = parsed._replace(query="[redacted]", fragment="")
            return parsed.geturl()
        except Exception:
            return "[redacted]"

    def load_from_file(self, filepath: str) -> int:
        """从文件加载 IP 列表"""
        path = Path(filepath)
        if not path.is_file():
            print(f"[PROXY] 代理文件不存在: {filepath}", file=sys.stderr)
            return 0
        
        nodes = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    norm = self._normalize_proxy(line)
                    if norm:
                        nodes.append(ProxyNode(url=norm))
        
        self._nodes = nodes
        return len(nodes)

    async def start_background_tasks(self) -> None:
        """启动后台任务（例如 API 定时拉取）"""
        if self.api_url and self._api_task is None:
            self._api_task = asyncio.create_task(self._api_refresh_loop())

    async def stop_background_tasks(self) -> None:
        """停止后台任务"""
        if self._api_task:
            self._api_task.cancel()
            try:
                await self._api_task
            except asyncio.CancelledError:
                pass
            self._api_task = None

    async def _api_refresh_loop(self) -> None:
        """后台定时从 API 拉取新代理"""
        while True:
            try:
                await self.refresh_from_api()
            except Exception as e:
                print(f"[PROXY_API] 提取失败: {type(e).__name__}", file=sys.stderr)
            await asyncio.sleep(self.api_refresh_sec)

    async def refresh_from_api(self) -> int:
        """调用 API 获取 IP 列表"""
        if not self.api_url or not httpx:
            return 0
        
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(self.api_url)
            if resp.status_code != 200:
                print(f"[PROXY_API] HTTP {resp.status_code}", file=sys.stderr)
                return 0
            
            lines = resp.text.strip().splitlines()
            new_nodes = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    norm = self._normalize_proxy(line)
                    new_nodes.append(ProxyNode(url=norm))
            
            if new_nodes:
                async with self._lock:
                    # 保留未过期的旧节点，合并新节点
                    valid_old = [n for n in self._nodes if n.is_available]
                    urls = {n.url for n in valid_old}
                    for n in new_nodes:
                        if n.url not in urls:
                            valid_old.append(n)
                            urls.add(n.url)
                    self._nodes = valid_old
                print(f"[PROXY_API] 刷新成功: 当前池大小 {len(self._nodes)}")
                return len(new_nodes)
        return 0

    async def get_proxy(self) -> Optional[str]:
        """获取一个可用的代理 URL。支持隧道、轮询与粘性控制。"""
        # 1. 隧道模式：最简单，直接返回隧道地址
        if self.tunnel:
            return self.tunnel

        # 2. 直连模式
        if not self._nodes:
            if self.api_url:
                await self.refresh_from_api()
            if not self._nodes:
                return None

        # 3. 内存代理池轮询
        async with self._lock:
            now = time.time()
            candidates = [n for n in self._nodes if n.is_available]
            if not candidates:
                # 若全部被冷却，解除最早冷却的那个以防死锁
                self._nodes.sort(key=lambda x: x.cooldown_until)
                candidates = self._nodes[:max(1, len(self._nodes) // 5)]
            
            # 选择粘性请求未耗尽的节点或轮询下一个
            node = candidates[self._index % len(candidates)]
            self._index = (self._index + 1) % len(candidates)
            
            node.used_times += 1
            node.in_use_count += 1
            return node.url

    async def report_result(self, proxy_url: Optional[str], success: bool, is_cf_block: bool = False) -> None:
        """反馈代理使用结果，自动进行降权或冷却"""
        if not proxy_url or self.tunnel:
            return

        async with self._lock:
            for node in self._nodes:
                if node.url == proxy_url:
                    node.in_use_count = max(0, node.in_use_count - 1)
                    if success:
                        node.success_count += 1
                        node.fail_count = 0
                    else:
                        node.fail_count += 1
                        # 如果是 Cloudflare 封锁或多次失败，立即打入冷却
                        penalty = self.cooldown_sec * (2 if is_cf_block else 1)
                        node.cooldown_until = time.time() + penalty
                    break

    @property
    def total_count(self) -> int:
        return 1 if self.tunnel else len(self._nodes)

    @property
    def active_count(self) -> int:
        if self.tunnel:
            return 1
        return sum(1 for n in self._nodes if n.is_available)

    def reload_config(self, config: dict) -> None:
        """动态重载代理配置"""
        mode = config.get("mode", "direct").lower()
        tunnel = config.get("tunnel") or None
        proxy_file = config.get("proxy_file") or None
        api_url = config.get("api_url") or None

        if mode == "tunnel" and tunnel:
            self.tunnel = self._normalize_proxy(tunnel)
            self.proxy_file = None
            self.api_url = None
            self._nodes = []
        elif mode == "file" and proxy_file:
            self.tunnel = None
            self.proxy_file = proxy_file
            self.api_url = None
            self.load_from_file(proxy_file)
        elif mode == "api" and api_url:
            self.tunnel = None
            self.proxy_file = None
            self.api_url = api_url
            self._nodes = []
        else:
            self.tunnel = None
            self.proxy_file = None
            self.api_url = None
            self._nodes = []

        self.sticky_requests = int(config.get("sticky_requests", 20))
        self.cooldown_sec = float(config.get("cooldown_sec", 60.0))
        self._updated_at = float(config.get("updated_at", 0))

    @classmethod
    def from_config(cls, config: dict) -> "ProxyManager":
        mode = config.get("mode", "direct").lower()
        tunnel = config.get("tunnel") if mode == "tunnel" else None
        proxy_file = config.get("proxy_file") if mode == "file" else None
        api_url = config.get("api_url") if mode == "api" else None
        mgr = cls(
            tunnel=tunnel,
            proxy_file=proxy_file,
            api_url=api_url,
            sticky_requests=int(config.get("sticky_requests", 20)),
            cooldown_sec=float(config.get("cooldown_sec", 60.0)),
        )
        mgr._updated_at = float(config.get("updated_at", 0))
        return mgr

    @classmethod
    def from_redis(cls, r=None, key: str = "tps:config:proxy") -> "ProxyManager":
        cfg = load_proxy_config(r, key=key)
        return cls.from_config(cfg)

    def check_and_reload(self, r, key: str = "tps:config:proxy") -> bool:
        """如果 Redis 中的配置更新，则执行热重载"""
        try:
            cfg = load_proxy_config(r, key=key)
            if float(cfg.get("updated_at", 0)) > getattr(self, "_updated_at", 0):
                self.reload_config(cfg)
                return True
        except Exception:
            pass
        return False


class StickyLanes:
    """一条 IP 绑定一个浏览器，429 后冷却，再换一条休息好的。"""

    def __init__(self, proxies: List[str], rest_sec: float = IP_REST_SEC):
        nodes = []
        seen = set()
        for raw in proxies:
            url = ProxyManager._normalize_proxy(raw)
            if not url or url in seen:
                continue
            seen.add(url)
            nodes.append(url)
        if not nodes:
            raise ValueError("sticky lanes need at least one proxy")
        self.rest_sec = float(rest_sec)
        self._urls = nodes
        self._rest_until = {url: 0.0 for url in nodes}
        self._holder: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return len(self._urls)

    def merge_urls(self, urls) -> int:
        """Merge fresh upstream exits into the pool without dropping hot lanes.

        New exits start rested (rest_until=0) so the next 429 can switch to
        them immediately. Returns how many exits were added.
        """
        added = 0
        with self._lock:
            for raw in urls or []:
                url = ProxyManager._normalize_proxy(raw)
                if not url or url in self._urls or url in self._rest_until:
                    continue
                self._urls.append(url)
                self._rest_until[url] = 0.0
                added += 1
        return added

    @property
    def shared_host(self) -> bool:
        # 隧道拆分的多条出口共享同一网关 host，429 可能是账号总量；独立 IP 文件各 host 不同
        hosts = set()
        for url in self._urls:
            try:
                raw = (url or "").strip()
                if not raw:
                    continue
                if "://" not in raw:
                    raw = "http://" + raw
                hostname = urlparse(raw).hostname
                if hostname:
                    hosts.add(hostname.lower())
            except Exception:
                continue
        return len(hosts) <= 1

    def holder_url(self, holder: str) -> Optional[str]:
        with self._lock:
            return self._holder.get(holder)

    def checkout(self, holder: str) -> Optional[str]:
        """这个浏览器已有的 IP 还可用就继续用，否则领一条空闲的。"""
        with self._lock:
            return self._checkout_locked(holder)

    def cool(self, holder: str, rest_sec: Optional[float] = None) -> Optional[str]:
        """把这个浏览器当前的 IP 冷却，并改绑下一条休息好的。

        没有下一条时保持原绑定并返回 None。调用方暂停领取，直到这条 IP 休息完。
        """
        with self._lock:
            current = self._holder.get(holder)
            if current:
                rest = self.rest_sec if rest_sec is None else float(rest_sec)
                self._rest_until[current] = time.time() + rest
                self._holder.pop(holder, None)
            nxt = self._checkout_locked(holder)
            if nxt:
                return nxt
            if current:
                self._holder[holder] = current
            return None

    def holder_rest(self, holder: str) -> float:
        """这个浏览器手里的 IP 还要冷却多久。"""
        with self._lock:
            url = self._holder.get(holder)
            if not url:
                return self._soonest_unlocked()
            return max(0.0, self._rest_until.get(url, 0.0) - time.time())

    def seconds_until_ready(self) -> float:
        """下一条没人占用的 IP 还要休息多久。已经有空闲 IP 时是 0。"""
        with self._lock:
            return self._soonest_unlocked()

    def _soonest_unlocked(self) -> float:
        now = time.time()
        held = set(self._holder.values())
        soonest = None
        for url in self._urls:
            if url in held:
                continue
            wait = self._rest_until[url] - now
            if wait <= 0:
                return 0.0
            if soonest is None or wait < soonest:
                soonest = wait
        return 0.0 if soonest is None else soonest

    def _checkout_locked(self, holder: str) -> Optional[str]:
        current = self._holder.get(holder)
        now = time.time()
        if current and now >= self._rest_until.get(current, 0.0):
            return current
        self._holder.pop(holder, None)
        held = set(self._holder.values())
        for url in self._urls:
            if url in held:
                continue
            if now < self._rest_until[url]:
                continue
            self._holder[holder] = url
            return url
        return None

    @classmethod
    def from_file(cls, filepath: str, rest_sec: float = IP_REST_SEC) -> "StickyLanes":
        path = Path(filepath)
        proxies = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line)
        return cls(proxies, rest_sec=rest_sec)


def fetch_proxy_list(api_url: str, timeout: float = 30.0) -> list:
    """Pull fresh exits from an upstream provider API.

    Accepts plain text (one proxy per line) or JSON (a list of strings,
    or an object with a list under data/list/proxies/ips/hosts/results;
    items may be strings or {host, port, user, pass} objects).
    Returns normalized, de-duplicated proxy URLs, order preserved.
    Raises RuntimeError with the masked URL on any failure.
    """
    raw = (api_url or "").strip()
    if not raw:
        raise RuntimeError("proxy API URL is empty")
    masked = ProxyManager._mask_proxy(raw)
    try:
        request = urllib.request.Request(raw, headers={"User-Agent": "tps-proxy-pool/1"})
        with urllib.request.urlopen(request, timeout=max(5.0, float(timeout))) as response:
            body = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"proxy API fetch failed ({masked}): {type(exc).__name__}") from exc
    candidates = _extract_proxy_candidates(body)
    urls = []
    seen = set()
    for item in candidates:
        url = ProxyManager._normalize_proxy(item)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        raise RuntimeError(f"proxy API returned no usable exits ({masked})")
    return urls


def _extract_proxy_candidates(body: str) -> list:
    text = (body or "").strip()
    if not text:
        return []
    if text[:1] in ("[", "{"):
        try:
            return _candidates_from_json(json.loads(text))
        except Exception:
            pass
    out = []
    for line in text.splitlines():
        line = line.strip().strip("\"'")
        if not line or line.startswith("#"):
            continue
        if "://" in line or ":" in line:
            out.append(line)
    return out


def _candidates_from_json(data) -> list:
    if isinstance(data, str):
        return [data]
    if isinstance(data, dict):
        for key in ("data", "list", "proxies", "ips", "hosts", "results", "items"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
        else:
            return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            host = item.get("host") or item.get("ip") or item.get("server") or ""
            port = item.get("port") or ""
            if host and port:
                user = item.get("user") or item.get("username") or ""
                password = item.get("pass") or item.get("password") or ""
                auth = f"{user}:{password}@" if user else ""
                scheme = item.get("scheme") or item.get("protocol") or "http"
                out.append(f"{scheme}://{auth}{host}:{port}")
            elif host:
                out.append(str(host))
    return out


def sticky_gateway_url(proxy_url: str, holder: str, minutes: int = 120) -> str:
    """给 region-US 这种轮换用户名加上 sid，同一 holder 粘住同一出口。"""
    raw = (proxy_url or "").strip()
    if not raw:
        return raw
    parts = urlparse(raw if "://" in raw else "http://" + raw)
    user = parts.username or ""
    if not user or "-sid-" in user.lower() or "-region-" not in user.lower():
        return raw
    sid = re.sub(r"[^A-Za-z0-9]", "", str(holder))[:16] or "lane"
    hold = max(1, min(int(minutes), 120))
    username = f"{user}-sid-{sid}-t-{hold}"
    password = parts.password or ""
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return urlunparse((parts.scheme or "http", f"{auth}@{host}{port}", parts.path or "", "", "", ""))


def refresh_sticky_url(proxy_url: str, minutes: int = 120) -> str:
    """Return the same gateway with a new random sid.

    Usernames containing -region- (ZooProxy/Cliproxy) lose any existing
    -sid-<token>-t-<number> segment, then gain
    -sid-<8 lowercase letters/digits>-t-<minutes clamped 1..120>.
    Scheme, password, host, and port stay. No -region- marker: unchanged.
    """
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
    return urlunparse((parts.scheme or "http", f"{auth}@{host_str}{port}", parts.path or "", "", "", ""))


def tunnel_sticky_lanes(proxy_url: str, count: int = 2, rest_sec: float = IP_REST_SEC) -> Optional[StickyLanes]:
    """一条动态网关拆成多条粘性出口。拆不开时仍作为一条隧道。"""
    urls = []
    seen = set()
    for i in range(max(1, int(count))):
        url = sticky_gateway_url(proxy_url, f"lane{i}")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        return None
    return StickyLanes(urls, rest_sec=rest_sec)


def sticky_lanes_from_config(config: dict) -> Optional[StickyLanes]:
    """只接受代理文件。隧道每次请求换出口，不能拿来做粘性循环。"""
    if (config.get("mode") or "").lower() != "file":
        return None
    path = config.get("proxy_file") or ""
    if not path or not Path(path).is_file():
        return None
    return StickyLanes.from_file(path)


# ============================================================
# 配置持久化与脱敏工具函数
# ============================================================

REDIS_PROXY_CONFIG_KEY = "tps:config:proxy"
LOCAL_PROXY_CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "proxy_config.json"


def _protect_proxy_secret_file(path: Path) -> None:
    """Restrict a config file before any credential bytes are written to it."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return

    import ctypes
    from ctypes import wintypes

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    whoami = system_root / "System32" / "whoami.exe"
    result = subprocess.run(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    rows = list(csv.reader(result.stdout.splitlines()))
    if len(rows) != 1 or len(rows[0]) < 2 or not re.fullmatch(r"S-1-(?:\d+-)+\d+", rows[0][1]):
        raise RuntimeError("Could not identify the current Windows user for proxy config ACL")
    sid = rows[0][1]
    sddl = f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None,
    ):
        raise OSError("Could not create protected proxy config ACL")
    try:
        dacl = ctypes.c_void_p()
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        if not advapi.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted),
        ) or not present.value or not dacl.value:
            raise OSError("Could not resolve protected proxy config ACL")
        # Set DACL and protected-inheritance flag in one Windows operation.
        status = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
        if status != 0:
            raise OSError("Could not protect proxy config file")
    finally:
        kernel32.LocalFree(descriptor)

# Only restore our own write. A newer writer must never be overwritten by
# recovery from a failed local-file replacement.
_ROLLBACK_PROXY_CONFIG_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == '1' then
    redis.call('SET', KEYS[1], ARGV[3])
else
    redis.call('DEL', KEYS[1])
end
return 1
"""


@contextmanager
def _proxy_config_file_lock(config_path: Path):
    """Serialize local writers across processes without putting secrets in the lock file."""
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    locked = False
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def default_proxy_config() -> dict:
    return {
        "mode": "direct",          # "tunnel" | "file" | "api" | "direct"
        "tunnel": "",              # 隧道代理 URL
        "proxy_file": "",          # 本地代理文件路径
        "api_url": "",             # API 提取 URL
        "sticky_requests": 20,     # 单 IP 粘性请求次数
        "cooldown_sec": 60.0,      # 失败冷却时间
        "updated_at": 0.0,
    }


def save_proxy_config(r=None, config: dict = None, key: str = REDIS_PROXY_CONFIG_KEY) -> dict:
    """Stage a private file, then save Redis and atomically replace the file."""
    import json
    cfg = default_proxy_config()
    if config:
        cfg.update(config)
    cfg["updated_at"] = time.time()

    # 规范化 tunnel
    if cfg.get("tunnel"):
        cfg["tunnel"] = ProxyManager._normalize_proxy(cfg["tunnel"])

    payload_str = json.dumps(cfg, ensure_ascii=False, indent=2)
    redis_payload = json.dumps(cfg, ensure_ascii=False)

    try:
        LOCAL_PROXY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _proxy_config_file_lock(LOCAL_PROXY_CONFIG_FILE):
            return _save_proxy_config_locked(r, key, cfg, payload_str, redis_payload)
    except RuntimeError:
        raise
    except Exception as e:
        print(f"[PROXY] 保存配置失败: {type(e).__name__}", file=sys.stderr)
        raise RuntimeError("代理配置未能完整保存") from None


def _save_proxy_config_locked(r, key: str, cfg: dict, payload_str: str, redis_payload: str) -> dict:
    """The lock covers both stores and any conditional Redis rollback."""

    # Stage the private file before changing Redis. If staging fails, neither
    # copy has changed; if the final replacement fails, restore Redis below.
    temporary_path = None
    previous_redis_value = None
    redis_write_attempted = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=LOCAL_PROXY_CONFIG_FILE.parent,
            prefix=".proxy_config.", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            _protect_proxy_secret_file(temporary_path)
            temporary.write(payload_str)
            temporary.flush()
            os.fsync(temporary.fileno())

        if r is not None:
            previous_redis_value = r.get(key)
            redis_write_attempted = True
            if not r.set(key, redis_payload):
                raise RuntimeError("Redis set failed")

        os.replace(temporary_path, LOCAL_PROXY_CONFIG_FILE)
        temporary_path = None
        return cfg
    except Exception as e:
        print(f"[PROXY] 保存配置失败: {type(e).__name__}", file=sys.stderr)
        if r is not None and redis_write_attempted:
            try:
                restored = r.eval(
                    _ROLLBACK_PROXY_CONFIG_LUA, 1, key, redis_payload,
                    1 if previous_redis_value is not None else 0,
                    previous_redis_value if previous_redis_value is not None else "",
                )
                if restored != 1:
                    print("[PROXY] Redis 配置已被其他写入更新，跳过回滚", file=sys.stderr)
            except Exception as rollback_error:
                print(f"[PROXY] Redis 回滚失败: {type(rollback_error).__name__}", file=sys.stderr)
        raise RuntimeError("代理配置未能完整保存") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def load_proxy_config(r=None, key: str = REDIS_PROXY_CONFIG_KEY) -> dict:
    """优先从 Redis 读取配置，若不可用则从本地文件读取"""
    import json
    cfg = default_proxy_config()

    # 1. 尝试从 Redis 读取
    if r is not None:
        try:
            raw = r.get(key)
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    cfg.update(data)
                    return cfg
        except Exception:
            pass

    # 2. 从本地文件读取
    if LOCAL_PROXY_CONFIG_FILE.is_file() and not LOCAL_PROXY_CONFIG_FILE.is_symlink():
        try:
            _protect_proxy_secret_file(LOCAL_PROXY_CONFIG_FILE)
            raw = LOCAL_PROXY_CONFIG_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                cfg.update(data)
                return cfg
        except Exception:
            pass

    return cfg


def mask_proxy_config(config: dict) -> dict:
    """脱敏配置供前端展示（隐藏账号密码）"""
    safe = {key: config.get(key) for key in (
        "mode", "proxy_file", "sticky_requests", "cooldown_sec", "updated_at",
    ) if key in config}
    safe["api_url"] = "********" if config.get("api_url") else ""
    tunnel = config.get("tunnel") or ""
    if tunnel:
        safe["tunnel_masked"] = ProxyManager._mask_proxy(tunnel)
        safe["tunnel"] = safe["tunnel_masked"]
        # 如果包含密码，把原始密码掩码
        try:
            parsed = urlparse(tunnel)
            safe["host"] = parsed.hostname or ""
            safe["port"] = parsed.port or ""
            safe["username"] = parsed.username or ""
            safe["has_password"] = bool(parsed.password)
        except Exception:
            safe["tunnel_masked"] = "[redacted]"
            safe["tunnel"] = "[redacted]"
    else:
        safe["tunnel_masked"] = ""
        safe["tunnel"] = ""
        safe["has_password"] = False
    return safe
