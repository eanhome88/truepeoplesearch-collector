#!/usr/bin/env python3
"""账号级 lane 配额隔离（纯内存 + 标准库）。

背景：429 是 per-account 的——穿云同账号多出口共享配额，单账号下堆
lane 数量线性无效；加第 2 个账号时必须是插拔式（只 merge 新出口，
不改旧绑定、不重启 worker）。

本模块只做一件事：按网关账号（代理 URL 的用户名，去掉 sid/session
等粘性后缀后归一化）维护独立配额桶——QPS 上限可配、429 熔断各算各的；
lane 与账号的绑定关系可查；取 lane 时自动跳过被熔断账号的全部出口。

worker 接入补丁草案（仅草案，不改现有文件；落点均为
scripts/distributed_worker.py::LeaseWorker）：
  1. LeaseWorker.__init__ 内、load_worker_lanes 之后加：
       from account_lanes import AccountLanes
       urls = list(lanes._urls) if lanes is not None else []
       self.accounts = AccountLanes(urls, qps_per_account=5.0, fuse_sec=40.0)
     StickyLanes 保留（管单 IP 冷却），AccountLanes 叠在上层（管账号熔断）。
  2. _switch_ip / _on_rate_limit 内、判定 429 后加：
       acct = self.accounts.report_429(group.proxy or "")
       # 同账号其他 lane 自动不可用；切道时取数改为：
       nxt = self.accounts.checkout(str(group.gid))
     checkout 已跳过熔断账号，返回 None 时再走原有全局暂停逻辑。
  3. 第二账号上线时（插拔式，无需重启）：
       added = self.accounts.merge_urls(new_urls)  # 新账号桶自动创建
       # 旧 holder 绑定不变；新 checkout 才会用到新账号的 lane。
  4. 成功路径无需调用本模块（熔断只由 report_429 触发、到期自动恢复；
     QPS 窗口随时间自动滑出）。

线程安全：内部统一加锁；时间源可注入，便于单测用 Fake 时钟。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional
from urllib.parse import urlparse, unquote


_SID_RE = re.compile(r"(?i)-sid-[A-Za-z0-9_-]+(?:-t-\d+)?")
_SESSION_RE = re.compile(r"(?i)[-_]session[-_][A-Za-z0-9_-]+")
_CB_RE = re.compile(r"(?i)_s[A-Za-z0-9]+(?:-\d+m)?$")


def _base_account(username: str) -> str:
    """去掉粘性后缀（-sid-xxx-t-NN / -session_xxx / _s...-30m），同网关账号归一桶。"""
    base = username or ""
    base = _CB_RE.sub("", base)
    base = _SID_RE.sub("", base)
    base = _SESSION_RE.sub("", base)
    return base.strip().lower()


class AccountLanes:
    """按网关账号隔离配额的 lane 表。

    - 每个账号独立 QPS 滑动窗口 + 独立 429 熔断计时；
    - holder（浏览器组 id）绑定某条 lane，checkout 优先保活；
    - 熔断账号的 lane 在取数时整体跳过，熔断到期自动恢复；
    - merge_urls() 插拔式追加新账号出口，不碰旧绑定。
    """

    def __init__(
        self,
        proxies: List[str],
        qps_per_account: float = 5.0,
        fuse_sec: float = 40.0,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        if qps_per_account <= 0:
            raise ValueError("qps_per_account must be > 0")
        if fuse_sec < 0:
            raise ValueError("fuse_sec must be >= 0")
        self.qps = float(qps_per_account)
        self.fuse_sec = float(fuse_sec)
        self._now: Callable[[], float] = now or time.monotonic
        self._lock = threading.Lock()
        self._urls: List[str] = []
        self._holder: Dict[str, str] = {}
        self._fused_until: Dict[str, float] = {}
        self._takes: Dict[str, Deque[float]] = {}
        self.merge_urls(proxies or [])

    @staticmethod
    def account_of(proxy_url: str) -> str:
        """代理 URL -> 归一化账号；无用户名时退化为 host，保证总有桶。"""
        raw = (proxy_url or "").strip()
        if not raw:
            return ""
        try:
            parts = urlparse(raw if "://" in raw else "http://" + raw)
            user = unquote(parts.username or "")
        except Exception:
            return ""
        base = _base_account(user)
        if base:
            return base
        try:
            return (parts.hostname or "").lower()
        except Exception:
            return ""

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._urls)

    @property
    def accounts(self) -> List[str]:
        with self._lock:
            return sorted({self.account_of(u) for u in self._urls})

    def merge_urls(self, urls) -> int:
        """插拔式追加出口（含新账号）；已存在的不重复；返回新增条数。"""
        added = 0
        with self._lock:
            for raw in urls or []:
                url = (raw or "").strip()
                if not url or url in self._urls:
                    continue
                self._urls.append(url)
                added += 1
        return added

    def holder_url(self, holder: str) -> Optional[str]:
        with self._lock:
            return self._holder.get(str(holder))

    def lane_account(self, proxy_url: str) -> str:
        """lane 与账号绑定表：单条 lane 查账号（无锁纯函数，可直接调）。"""
        return self.account_of(proxy_url)

    def is_fused(self, account: str) -> bool:
        with self._lock:
            return self._now() < self._fused_until.get(account, 0.0)

    def account_state(self, account: str) -> dict:
        """观测单个账号桶：熔断剩余秒数 + 最近 1s 窗口内已用配额。"""
        with self._lock:
            now = self._now()
            takes = self._takes.get(account, deque())
            recent = sum(1 for t in takes if now - t < 1.0)
            return {
                "account": account,
                "fused_remaining_sec": max(0.0, self._fused_until.get(account, 0.0) - now),
                "takes_last_sec": recent,
                "qps": self.qps,
            }

    def checkout(self, holder: str) -> Optional[str]:
        """取一条可用 lane：保活优先，否则按顺序找未熔断、QPS 未满的空闲 lane。

        熔断账号的 lane（含 holder 手里那条）一律跳过；全部不可用返回 None。
        新绑定消耗一次该账号 QPS 配额（保活续用不重复消耗）。
        """
        key = str(holder)
        with self._lock:
            now = self._now()
            current = self._holder.get(key)
            if current and self._usable_locked(current, now, for_holder=key):
                return current
            if current:
                self._holder.pop(key, None)
            held = set(self._holder.values())
            for url in self._urls:
                if url in held:
                    continue
                if not self._usable_locked(url, now, for_holder=None):
                    continue
                self._holder[key] = url
                self._takes.setdefault(self.account_of(url), deque()).append(now)
                return url
            if current and self._account_ok_locked(self.account_of(current), now):
                # 无空闲 lane 但原账号健康：保活原绑定，不额外耗配额。
                self._holder[key] = current
                return current
            return None

    def report_429(self, proxy_or_holder: str) -> str:
        """上报一次 429：熔断该 lane 所属的整个账号 fuse_sec 秒。

        入参可传 lane URL，也可传 holder id（自动解出其绑定 lane）。
        返回被熔断的账号；空字符串表示无有效账号。
        """
        with self._lock:
            token = (proxy_or_holder or "").strip()
            url = self._holder.get(token, token)
            account = self.account_of(url)
            if not account:
                return ""
            self._fused_until[account] = self._now() + self.fuse_sec
            if url in self._holder.values():
                for holder, bound in list(self._holder.items()):
                    if bound == url:
                        self._holder.pop(holder, None)
            else:
                self._holder.pop(token, None)
            return account

    def _account_ok_locked(self, account: str, now: float) -> bool:
        return bool(account) and now >= self._fused_until.get(account, 0.0)

    def _qps_ok_locked(self, account: str, now: float) -> bool:
        takes = self._takes.get(account)
        if takes is None:
            return True
        while takes and now - takes[0] >= 1.0:
            takes.popleft()
        return len(takes) < self.qps

    def _usable_locked(self, url: str, now: float, for_holder: Optional[str]) -> bool:
        account = self.account_of(url)
        if not self._account_ok_locked(account, now):
            return False
        if for_holder is not None:
            return True  # 保活续用不耗配额
        return self._qps_ok_locked(account, now)
