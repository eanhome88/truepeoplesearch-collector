#!/usr/bin/env python3
"""按真实出口 IP 亲和的会话守卫（egress guard）。

背景：sticky 代理行 10 分钟可跳 3 个出口 IP，cf_clearance 绑 IP，
会话按代理行 key 必失效。做法：lane 拿会话前经**同代理**发一次轻量
出口 IP 探测（超时 5s，失败即放行不阻塞），出口 IP 变了就标记该 lane
会话过期，触发重解。

只用标准库（urllib / ipaddress / threading / os），不新增第三方依赖。
本模块不 import distributed_worker / cf_farm，无导入环，可被单测直接引用。

worker 最小接入点（共 2 个，不改 scripts/cf_farm.py）：

  接入点 1 — browser 模式：``_ChromeBox.ensure_session``
    复用旧 session 前探一次，变了就关掉旧会话（下次建新会话=重解）：

    .. code-block:: python

        # distributed_worker.py, _ChromeBox.ensure_session 开头
        from egress_guard import default_guard
        # lane 键用 group.gid 字符串（与 StickyLanes holder 一致）
        changed = await asyncio.to_thread(
            default_guard().check_lane, str(self._lane_id), self.proxy)
        if changed:
            await self.close()

  接入点 2 — OWN_CF 模式：``_fetch_page_own_cf``
    challenged 判定前把 egress 变化折成过期，复用已有重解分支：

    .. code-block:: python

        # distributed_worker.py, _fetch_page_own_cf 内 session.get 之前
        from egress_guard import expire_own_cf_session_if_egress_moved
        expire_own_cf_session_if_egress_moved(
            lane_id=str(group_gid), proxy=session.proxy, session=session)
        # 该 helper 在出口变化时置 session.expires_at=0，
        # 后续 challenged=True 走现有 session.resolve(url) 重解分支。

单测见 scripts/test_egress_guard.py（手写 Fake 探针，无真实网络）。
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Optional

PROBE_TIMEOUT_SEC = 5.0
DEFAULT_PROBE_URL = "https://api.ipify.org"
_ENV_TIMEOUT = "EGRESS_PROBE_TIMEOUT_SEC"
_ENV_URL = "EGRESS_PROBE_URL"
_ENV_DISABLE = "EGRESS_GUARD_DISABLE"

# 探针签名：(proxy_url, timeout_sec) -> 出口 IP 字符串，失败返回 ""。
ProbeFn = Callable[[str, float], str]


def _env_timeout(default: float = PROBE_TIMEOUT_SEC) -> float:
    try:
        val = float(os.environ.get(_ENV_TIMEOUT, default))
    except (TypeError, ValueError):
        return default
    return min(max(val, 1.0), 30.0)


def _env_probe_url(default: str = DEFAULT_PROBE_URL) -> str:
    return (os.environ.get(_ENV_URL) or default).strip() or default


def _env_disabled() -> bool:
    return (os.environ.get(_ENV_DISABLE) or "").strip() == "1"


def probe_egress_ip(proxy_url: str,
                    timeout: Optional[float] = None,
                    probe_url: Optional[str] = None) -> str:
    """经同代理 GET 轻量 IP 回显页，返回出口 IP；任何失败返回 ""（放行不阻塞）。

    只用标准库 urllib。调用方必须传 lane 正在用的同一条 proxy_url，
    否则探到的不是该 lane 的出口，亲和判断无意义。
    """
    raw_proxy = (proxy_url or "").strip()
    if not raw_proxy:
        return ""
    to = float(timeout) if timeout else _env_timeout()
    target = (probe_url or _env_probe_url()).strip()
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": raw_proxy, "https": raw_proxy}))
        req = urllib.request.Request(
            target, method="GET", headers={"User-Agent": "tps-egress-guard/1"})
        with opener.open(req, timeout=to) as resp:
            body = resp.read(256).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    token = body.split()[0] if body.split() else ""
    try:
        ipaddress.ip_address(token)
    except ValueError:
        return ""
    return token


@dataclass
class EgressCheck:
    lane: str
    changed: bool       # True = 出口变了，调用方应把该 lane 会话当过期触发重解
    ip: str             # 本次探到的出口 IP（探测失败时为上次记录值或 ""）
    probed: bool        # 本次是否成功探到 IP


class EgressGuard:
    """记录 lane -> 出口 IP；``check_lane`` 变了返回 True。线程安全。"""

    def __init__(self,
                 probe: Optional[ProbeFn] = None,
                 timeout: Optional[float] = None,
                 probe_url: Optional[str] = None,
                 enabled: Optional[bool] = None):
        self._probe: ProbeFn = probe or probe_egress_ip
        self._timeout = float(timeout) if timeout else _env_timeout()
        self._probe_url = (probe_url or _env_probe_url()).strip()
        self._enabled = (not _env_disabled()) if enabled is None else bool(enabled)
        self._lock = threading.Lock()
        self._last_ip: Dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def last_ip(self, lane: str) -> str:
        with self._lock:
            return self._last_ip.get(str(lane), "")

    def remember(self, lane: str, ip: str) -> None:
        """求解成功后记录当时出口（可选；check_lane 首探会自动记录）。"""
        ip = (ip or "").strip()
        if not ip:
            return
        with self._lock:
            self._last_ip[str(lane)] = ip

    def forget(self, lane: str) -> None:
        with self._lock:
            self._last_ip.pop(str(lane), None)

    def check_lane(self, lane: str, proxy_url: str) -> bool:
        """lane 拿会话前调用。出口变化返回 True（会话按过期重解），其余 False。"""
        return self.check(lane, proxy_url).changed

    def check(self, lane: str, proxy_url: str) -> EgressCheck:
        key = str(lane)
        if not self._enabled or not (proxy_url or "").strip():
            with self._lock:
                known = self._last_ip.get(key, "")
            return EgressCheck(lane=key, changed=False, ip=known, probed=False)
        try:
            ip = (self._probe(proxy_url, self._timeout) or "").strip()
        except Exception:
            ip = ""
        if not ip:
            # 探测失败（超时/代理抖动）：放行不阻塞，不动记录。
            with self._lock:
                known = self._last_ip.get(key, "")
            return EgressCheck(lane=key, changed=False, ip=known, probed=False)
        with self._lock:
            known = self._last_ip.get(key, "")
            if not known:
                self._last_ip[key] = ip
                return EgressCheck(lane=key, changed=False, ip=ip, probed=True)
            if ip == known:
                return EgressCheck(lane=key, changed=False, ip=ip, probed=True)
            self._last_ip[key] = ip
            return EgressCheck(lane=key, changed=True, ip=ip, probed=True)


_DEFAULT_GUARD: Optional[EgressGuard] = None
_DEFAULT_LOCK = threading.Lock()


def default_guard() -> EgressGuard:
    """worker 内共享的默认守卫（延迟单例，env 开关生效）。"""
    global _DEFAULT_GUARD
    with _DEFAULT_LOCK:
        if _DEFAULT_GUARD is None:
            _DEFAULT_GUARD = EgressGuard()
        return _DEFAULT_GUARD


def lane_session_expired(lane: str,
                         proxy_url: str,
                         guard: Optional[EgressGuard] = None) -> bool:
    """便捷函数：该 lane 会话是否因出口漂移而过期（探测失败一律 False）。"""
    return (guard or default_guard()).check_lane(lane, proxy_url)


def expire_own_cf_session_if_egress_moved(lane_id: str,
                                         proxy: Optional[str],
                                         session,
                                         guard: Optional[EgressGuard] = None) -> bool:
    """OWN_CF 接入 helper：出口变了就把 session.expires_at 打到过去，返回 True。

    ``_fetch_page_own_cf`` 的 challenged 分支已含 ``session.expired``，
    打过去后自动走现有 ``session.resolve(url)`` 重解，无需改分支逻辑。
    （不能置 0：expired 实现是 bool(expires_at) and now >= expires_at，
    0 会被判为“未设过期”而非过期。）
    session 为 None 或无 expires_at 属性时只返回判断结果，不抛错。
    """
    moved = lane_session_expired(str(lane_id), proxy or "", guard)
    if moved and session is not None:
        try:
            session.expires_at = time.time() - 1.0
        except (AttributeError, TypeError):
            pass
    return moved


def drop_box_session_if_egress_moved(box, lane_id: str,
                                    guard: Optional[EgressGuard] = None) -> bool:
    """browser 接入 helper（同步版）：出口变了就 ``box.session = None``。

    worker 补丁在 ``ensure_session`` 内调用后，原有 ``if self.session is None``
    建会话分支自然重解；warm 标记由调用方按需重置，这里不动。
    """
    proxy = getattr(box, "proxy", None) or ""
    moved = lane_session_expired(str(lane_id), proxy, guard)
    if moved and box is not None:
        try:
            box.session = None
        except (AttributeError, TypeError):
            pass
    return moved
