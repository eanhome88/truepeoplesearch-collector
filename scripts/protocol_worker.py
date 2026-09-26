#!/usr/bin/env python3
"""
TruePeopleSearch 协议层分布式高并发 Worker (Protocol Worker)

特点：
1. 纯 asyncio 异步事件驱动，单进程即可维持 50~200 个并发连接。
2. 配合动态代理池与 curl_cffi TLS 指纹模拟，完全消除对无头浏览器的依赖。
3. 内存占用仅 100~300MB，CPU 仅需 1~2 核，轻松达成单机日产 300 万条 (35~70 QPS)。
4. 无缝兼容 Redis 可靠租约队列 (tps: 机制) 与 TiDB 批量聚合入库。

使用：
  # 1. 隧道代理模式 (最推荐)
  python3 protocol_worker.py --mode worker --concurrency 50 --proxy-tunnel "http://user:pass@host:port"

  # 2. 代理文件轮换模式
  python3 protocol_worker.py --mode worker --concurrency 50 --proxy-file proxies.txt

  # 3. API 提取代理模式
  python3 protocol_worker.py --mode worker --concurrency 50 --proxy-api "http://api.proxy.com/get"

  # 投递任务 / 查看统计 / 回收过期
  python3 protocol_worker.py --mode feed --file urls.txt
  python3 protocol_worker.py --mode stats
  python3 protocol_worker.py --mode recover
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, unquote, urlparse, urlunparse

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import redis

from batch_ingest import BatchIngester
from protocol_fetcher import (
    CloudflareChallengeError,
    EmptyPageError,
    FetchTimeoutError,
    HttpError,
    ProtocolFetcher,
    ProxyError,
    ScrapeError,
)
import proxy_pool
from proxy_pool import ProxyManager
from tps_control import clear_worker_heartbeat, write_worker_heartbeat
from tps_metrics import get_metrics
from tps_queue import (
    LEASE_SEC,
    MAX_ATTEMPTS,
    ack,
    claim,
    drain_legacy,
    extract_person_id,
    feed,
    heartbeat,
    nack,
    queue_stats,
    release,
    recover_expired,
)

REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

HB_INTERVAL_SEC = 15
RECOVER_INTERVAL_SEC = 15
STATS_INTERVAL_SEC = 5
DEFAULT_CONCURRENCY = 50
DAILY_TARGET = 3_000_000
# Per-slot pause only. Do not sleep IP_REST_SEC (~70 minutes) here.
_SLOT_RELEASE_SLEEP_SEC = 3


def _mentions_captcha(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "captcha" in text or "internalcaptcha" in text


# -sid-<token>-t-<minutes> inside a region-gateway username.
_SID_HOLD_RE = re.compile(r"(?i)-sid-[A-Za-z0-9]+-t-\d+")
_SID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _release_needs_new_sid(reason: str) -> bool:
    text = (reason or "").lower()
    return "429" in text or "captcha" in text


def _split_proxy(proxy_url: str):
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    parts = urlparse(raw if "://" in raw else "http://" + raw)
    # 3.13 leaves userinfo percent-encoded; unquote so quote() runs once.
    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    return parts, user, password


def _rebuild_proxy_url(parts, username: str, password: str) -> str:
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return urlunparse((
        parts.scheme or "http",
        f"{auth}@{host}{port}",
        parts.path or "",
        parts.params or "",
        parts.query or "",
        parts.fragment or "",
    ))


def _sid_token(proxy_url: str) -> str:
    parsed = _split_proxy(proxy_url)
    if not parsed:
        return ""
    match = re.search(r"(?i)-sid-([A-Za-z0-9]+)-t-\d+", parsed[1])
    return match.group(1) if match else ""


def _strip_sticky_sid(proxy_url: str) -> str:
    """Remove -sid-...-t-N from the username. Leave other URLs unchanged."""
    raw = (proxy_url or "").strip()
    parsed = _split_proxy(raw)
    if not parsed:
        return raw
    parts, user, password = parsed
    if not user or not _SID_HOLD_RE.search(user):
        return raw
    return _rebuild_proxy_url(parts, _SID_HOLD_RE.sub("", user), password)


def _new_sid() -> str:
    return "".join(secrets.choice(_SID_ALPHABET) for _ in range(8))


def _minted_sid(proxy_url: str) -> str:
    """Prefer proxy_pool.refresh_sticky_url for the new sid token."""
    refresher = getattr(proxy_pool, "refresh_sticky_url", None)
    if callable(refresher):
        try:
            minted = refresher(proxy_url)
        except Exception:
            minted = None
        if isinstance(minted, str):
            sid = _sid_token(minted)
            if sid:
                return sid
    return _new_sid()


def _mint_slot_sticky(proxy_url: str) -> str:
    """Region gateway username gets -sid-<8 alnum>-t-120. Userinfo is quoted once."""
    raw = (proxy_url or "").strip()
    parsed = _split_proxy(raw)
    if not parsed:
        return raw
    parts, user, password = parsed
    if not user or "-region-" not in user.lower():
        return raw
    user = _SID_HOLD_RE.sub("", user)
    sid = _minted_sid(raw)
    previous = _sid_token(raw)
    if previous and sid.lower() == previous.lower():
        sid = _new_sid()
    return _rebuild_proxy_url(parts, f"{user}-sid-{sid}-t-120", password)


def connect_redis() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


class ProtocolWorker:
    """协议层高性能 Worker 协调器"""

    def __init__(
        self,
        r: redis.Redis,
        concurrency: int = DEFAULT_CONCURRENCY,
        target_per_day: int = DAILY_TARGET,
        proxy_manager: Optional[ProxyManager] = None,
        batch_size: int = 50,
        flush_interval: float = 1.0,
        decoupled_ingest: bool = False,
    ):
        self.r = r
        self.concurrency = max(1, int(concurrency))
        self.target_per_day = int(target_per_day)
        self.decoupled_ingest = decoupled_ingest
        self.worker_id = f"proto-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.m = get_metrics(r)
        self.proxy_mgr = proxy_manager or ProxyManager()
        self.fetcher = ProtocolFetcher()

        self.in_flight: Dict[str, dict] = {}
        # slot_id -> sticky proxy URL. The sid is the -sid- segment of that username.
        self._slot_proxies: Dict[int, str] = {}
        self.stopping = False
        self._lock = asyncio.Lock()
        self._bg_tasks: List[asyncio.Task] = []

        # 统计计数
        self._start_time = time.time()
        self._completed_count = 0
        self._last_completed_count = 0
        self._last_calc_time = time.time()
        self._current_qps = 0.0

        # 初始化批量入库器（仅在非解耦模式下直接连接 TiDB）
        self.batch_ingester = (
            None
            if decoupled_ingest
            else BatchIngester(
                batch_size=batch_size,
                flush_interval_sec=flush_interval,
                on_success=self._on_batch_success,
                on_failure=self._on_batch_failure,
            )
        )

    def _safe_incr(self, bucket: str) -> None:
        try:
            self.m.incr(bucket)
        except Exception:
            pass

    def _on_batch_success(self, jobs: List[dict]) -> None:
        """批量入库成功回调：批量执行 Redis ack"""
        for job in jobs:
            try:
                ack(self.r, job)
                self.m.incr("success")
                self._completed_count += 1
            except Exception as e:
                print(f"[ACK_ERR] {e}", file=sys.stderr)

    def _on_batch_failure(self, job: dict, exc: Exception) -> None:
        """单条入库失败回调：执行 nack 重新入队重试"""
        print(f"[INGEST_FAIL] person_id={job.get('person_id')} err={exc}", file=sys.stderr)
        try:
            nack(self.r, job, "ingest_error", retry=True)
            self.m.incr("error")
        except Exception as e:
            print(f"[NACK_ERR] {e}", file=sys.stderr)

    async def _proxy_for_slot(self, slot_id: int) -> Optional[str]:
        """轮转网关原样使用，不补 sid。每次请求由 ZooProxy 换出口。"""
        current = self._slot_proxies.get(slot_id)
        if current:
            return current
        proxy_url = await self.proxy_mgr.get_proxy()
        if not proxy_url:
            return None
        self._slot_proxies[slot_id] = proxy_url
        return proxy_url

    def _rotate_slot_sid(self, slot_id: int) -> None:
        """Swap this slot's sid before the next claim. Do not log the proxy URL."""
        current = self._slot_proxies.get(slot_id)
        if not current:
            return
        base = _strip_sticky_sid(current)
        minted = _mint_slot_sticky(base)
        if minted:
            self._slot_proxies[slot_id] = minted

    async def _release_rate_limited(
        self,
        job: dict,
        job_id: str,
        url: str,
        reason: str,
        slot_id: Optional[int] = None,
    ) -> None:
        """Put the job back on pending without incrementing attempts."""
        await asyncio.to_thread(release, self.r, job, reason)
        self._safe_incr("rate_limit")
        async with self._lock:
            self.in_flight.pop(job_id, None)
        print(f"[rate_limit] {url} returned to pending", flush=True)
        await asyncio.sleep(_SLOT_RELEASE_SLEEP_SEC)

    async def run(self) -> None:
        """启动 Worker 主事件循环"""
        print(f"============================================================")
        print(f"[PROTOCOL_WORKER] 启动成功: worker_id={self.worker_id}")
        print(f"[PROTOCOL_WORKER] 并发协程数: {self.concurrency}")
        print(f"[PROTOCOL_WORKER] 目标吞吐量: {self.target_per_day:,} 条/天 (约 {self.target_per_day/86400:.1f} QPS)")
        print(f"[PROTOCOL_WORKER] 代理池节点数: {self.proxy_mgr.total_count}")
        print(f"============================================================")

        # 注册信号处理
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal, sig)
            except NotImplementedError:
                pass  # Windows 兼容

        # 启动后台服务
        await self.proxy_mgr.start_background_tasks()
        if self.batch_ingester:
            await self.batch_ingester.start()
        self._bg_tasks.append(asyncio.create_task(self._heartbeat_loop()))
        self._bg_tasks.append(asyncio.create_task(self._recover_loop()))
        self._bg_tasks.append(asyncio.create_task(self._monitor_loop()))

        # 启动工作协程池
        workers = [
            asyncio.create_task(self._worker_loop(slot_id))
            for slot_id in range(self.concurrency)
        ]

        try:
            await asyncio.gather(*workers)
        finally:
            await self._shutdown()

    def _handle_signal(self, sig: int) -> None:
        signame = signal.Signals(sig).name
        if self.stopping:
            return
        print(f"\n[SIGNAL] 收到 {signame}，停止拉取新任务，正在平稳关闭在飞请求...")
        self.stopping = True

    async def _worker_loop(self, slot_id: int) -> None:
        """单个工作协程循环"""
        while not self.stopping:
            # 1. 从 Redis 原子领取任务 (BLMOVE)
            try:
                job = await asyncio.to_thread(claim, self.r, f"{self.worker_id}-{slot_id}")
            except Exception as e:
                print(f"[CLAIM_ERR] {e}", file=sys.stderr)
                await asyncio.sleep(1)
                continue

            if not job:
                await asyncio.sleep(0.2)
                continue

            job_id = str(job.get("id"))
            person_id = job.get("person_id")
            url = job.get("url") or f"https://www.truepeoplesearch.com/find/person/{person_id}"

            async with self._lock:
                self.in_flight[job_id] = job

            # 2. 获取代理并执行协议抓取（同一 slot 复用粘性 sid）
            proxy_url = await self._proxy_for_slot(slot_id)
            try:
                data = await self.fetcher.fetch_person(url, proxy=proxy_url)

                # 抓取成功，反馈代理并提交入库
                await self.proxy_mgr.report_result(proxy_url, success=True)
                if self.decoupled_ingest:
                    payload = json.dumps({"data": data, "job": job}, ensure_ascii=False)
                    await asyncio.to_thread(self.r.lpush, "tps:buffer:parsed", payload)
                    self._completed_count += 1
                else:
                    await self.batch_ingester.add(data, job)

            except EmptyPageError as empty_err:
                if _mentions_captcha(empty_err):
                    await self.proxy_mgr.report_result(proxy_url, success=False, is_cf_block=True)
                    await self._release_rate_limited(job, job_id, url, "captcha", slot_id)
                else:
                    # 404 或查无此人：标记完成，避免无休止重试
                    await self.proxy_mgr.report_result(proxy_url, success=True)
                    await asyncio.to_thread(ack, self.r, job)
                    self.m.incr("empty")
                    self._completed_count += 1

            except CloudflareChallengeError:
                await self.proxy_mgr.report_result(proxy_url, success=False, is_cf_block=True)
                await self._release_rate_limited(job, job_id, url, "cloudflare", slot_id)

            except (ProxyError, FetchTimeoutError) as net_err:
                if _mentions_captcha(net_err):
                    await self.proxy_mgr.report_result(proxy_url, success=False, is_cf_block=False)
                    await self._release_rate_limited(job, job_id, url, "captcha", slot_id)
                else:
                    # 代理或网络超时：惩罚代理，任务退回重试
                    bucket = getattr(net_err, "bucket", "timeout")
                    await self.proxy_mgr.report_result(proxy_url, success=False, is_cf_block=False)
                    await asyncio.to_thread(nack, self.r, job, bucket, retry=True)
                    self._safe_incr(bucket)

            except HttpError as http_err:
                status = int(getattr(http_err, "status", 0) or 0)
                if status == 429 or _mentions_captcha(http_err):
                    reason = "HTTP 429" if status == 429 else "captcha"
                    await self._release_rate_limited(job, job_id, url, reason, slot_id)
                else:
                    bucket = getattr(http_err, "bucket", "http_4xx")
                    await asyncio.to_thread(nack, self.r, job, bucket, retry=True)
                    self._safe_incr(bucket)

            except ScrapeError as sc_err:
                if _mentions_captcha(sc_err):
                    await self._release_rate_limited(job, job_id, url, "captcha", slot_id)
                else:
                    bucket = getattr(sc_err, "bucket", "error")
                    await asyncio.to_thread(nack, self.r, job, bucket, retry=True)
                    self._safe_incr(bucket)

            except Exception as unk_err:
                print(f"[UNEXPECTED_ERR] {url}: {unk_err}", file=sys.stderr)
                await asyncio.to_thread(nack, self.r, job, "unexpected_error", retry=True)
                self._safe_incr("retry")

            finally:
                async with self._lock:
                    self.in_flight.pop(job_id, None)

    async def _heartbeat_loop(self) -> None:
        """任务租约自动续期与 Worker 心跳"""
        while not self.stopping:
            try:
                await asyncio.sleep(HB_INTERVAL_SEC)
                async with self._lock:
                    inflight_jobs = list(self.in_flight.values())

                # 1. 为在飞任务批量续租
                for job in inflight_jobs:
                    try:
                        await asyncio.to_thread(heartbeat, self.r, job)
                    except Exception:
                        pass

                # 2. 向 Redis 面板控制写入 Worker 状态
                payload = {
                    "worker_id": self.worker_id,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "concurrency": self.concurrency,
                    "engine": "protocol (curl_cffi)",
                    "inflight": [
                        {"id": str(j.get("id")), "person_id": j.get("person_id")}
                        for j in inflight_jobs[:20]
                    ],
                    "status": "stopping" if self.stopping else "running",
                    "current_qps": round(self._current_qps, 2),
                    "capacity_per_day": int(self._current_qps * 86400) if self._current_qps > 0 else self.target_per_day,
                    "target_per_day": self.target_per_day,
                }
                await asyncio.to_thread(write_worker_heartbeat, self.r, payload)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[HB_ERR] {e}", file=sys.stderr)

    async def _recover_loop(self) -> None:
        """定期扫描并回收超时租约 (防死锁)"""
        while not self.stopping:
            try:
                await asyncio.sleep(RECOVER_INTERVAL_SEC)
                reclaimed = await asyncio.to_thread(recover_expired, self.r)
                if reclaimed > 0:
                    print(f"[RECOVER] 自动回收过期超时任务: {reclaimed} 条")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[RECOVER_ERR] {e}", file=sys.stderr)

    async def _monitor_loop(self) -> None:
        """实时输出运行吞吐与监控指标"""
        while not self.stopping:
            try:
                await asyncio.sleep(STATS_INTERVAL_SEC)
                # 检查 Redis 代理配置是否热更新
                if self.proxy_mgr.check_and_reload(self.r):
                    print(f"[{self.worker_id}] 代理配置已通过管理页面动态热更新")
                now = time.time()
                elapsed = now - self._last_calc_time
                done_delta = self._completed_count - self._last_completed_count

                qps = done_delta / elapsed if elapsed > 0 else 0.0
                self._current_qps = qps
                self._last_completed_count = self._completed_count
                self._last_calc_time = now

                async with self._lock:
                    inflight_len = len(self.in_flight)

                active_proxies = self.proxy_mgr.active_count
                daily_estimate = int(qps * 86400)

                print(
                    f"[STATS] 实时吞吐: {qps:5.1f} QPS | "
                    f"在飞请求: {inflight_len:3d} | "
                    f"可用代理: {active_proxies:3d} | "
                    f"总完成数: {self._completed_count:6d} | "
                    f"预估日产: {daily_estimate:,} 条/日"
                )

            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _shutdown(self) -> None:
        """安全关闭各组件，确保数据完全入库与状态清理"""
        print("[SHUTDOWN] 正在执行优雅停机...")

        # 1. 取消所有后台周期任务
        for t in self._bg_tasks:
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)

        # 2. 刷新入库缓冲区，落盘所有剩余数据
        if self.batch_ingester:
            await self.batch_ingester.stop()
        await self.proxy_mgr.stop_background_tasks()

        # 3. 将所有未决任务安全退回队列，禁止丢单
        async with self._lock:
            remaining = list(self.in_flight.values())
        if remaining:
            print(f"[SHUTDOWN] 正在将 {len(remaining)} 个未决任务安全回退至待处理队列...")
            for job in remaining:
                try:
                    await asyncio.to_thread(nack, self.r, job, "interrupted", retry=True)
                except Exception:
                    pass

        # 4. 清理 Worker 心跳
        try:
            await asyncio.to_thread(clear_worker_heartbeat, self.r, self.worker_id)
        except Exception:
            pass

        print("[SHUTDOWN] 退出完成，所有资源已安全释放。")


# ============================================================
# CLI 命令行入口
# ============================================================

def run_feed(filepath: str) -> None:
    r = connect_redis()
    try:
        drained = drain_legacy(r)
        if drained:
            print(f"[DRAIN] legacy={drained}")
    except Exception as exc:
        print(f"[DRAIN] {exc}", file=sys.stderr)

    with open(filepath, encoding="utf-8") as fh:
        urls = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]

    stats = feed(r, urls, seen_check=True)
    enqueued = int(stats.get("enqueued") or 0)
    deduped = int(stats.get("deduped") or 0)
    invalid = int(stats.get("invalid") or 0)
    print(f"[FEED] 已入队={enqueued} 去重过滤={deduped} 无效URL={invalid}")


def run_recover() -> None:
    r = connect_redis()
    n = recover_expired(r)
    print(f"[RECOVER] 成功回收超时未决任务: {n} 条")


def run_stats() -> None:
    r = connect_redis()
    qs = queue_stats(r)
    snap = get_metrics(r).snapshot()
    print("[STATS] 队列状态: " + json.dumps(qs, ensure_ascii=False, indent=2))
    print("[STATS] 抓取指标: " + json.dumps(snap, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TruePeopleSearch 协议层高性能 Worker (纯异步协程 + IP 代理池)",
    )
    parser.add_argument(
        "--mode",
        choices=["worker", "feed", "recover", "stats"],
        default="worker",
        help="运行模式 (worker|feed|recover|stats)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="并发协程数 (默认 50，单机推荐 40~100)",
    )
    parser.add_argument(
        "--proxy-tunnel",
        type=str,
        default=os.environ.get("PROXY_TUNNEL"),
        help="隧道代理地址 (如 http://user:pass@gate.proxy.com:8080)",
    )
    parser.add_argument(
        "--proxy-file",
        type=str,
        default=os.environ.get("PROXY_FILE"),
        help="本地代理列表文件路径 (每行一个代理)",
    )
    parser.add_argument(
        "--proxy-api",
        type=str,
        default=os.environ.get("PROXY_API"),
        help="代理提取 API 链接",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="TiDB 批量写入单次阈值 (默认 50)",
    )
    parser.add_argument(
        "--flush-interval",
        type=float,
        default=1.0,
        help="TiDB 批量写入自动刷盘时间间隔(秒，默认 1.0)",
    )
    parser.add_argument(
        "--file",
        type=str,
        help="--mode feed 时的 URL 列表文件",
    )
    parser.add_argument(
        "--target-per-day",
        type=int,
        default=DAILY_TARGET,
        help="计划每日抓取目标量 (默认 3,000,000)",
    )
    parser.add_argument(
        "--decoupled-ingest",
        action="store_true",
        help="启用解耦模式：数据直接写入 Redis 缓冲区，由 bulk_ingester_daemon 独立批量入库 (冲刺 3000万必选)",
    )

    args = parser.parse_args()

    if args.mode == "feed":
        if not args.file:
            print("错误: --mode feed 必须指定 --file <path>", file=sys.stderr)
            sys.exit(1)
        run_feed(args.file)

    elif args.mode == "recover":
        run_recover()

    elif args.mode == "stats":
        run_stats()

    elif args.mode == "worker":
        r = connect_redis()
        if not args.proxy_tunnel and not args.proxy_file and not args.proxy_api:
            proxy_mgr = ProxyManager.from_redis(r)
        else:
            proxy_mgr = ProxyManager(
                tunnel=args.proxy_tunnel,
                proxy_file=args.proxy_file,
                api_url=args.proxy_api,
            )
        worker = ProtocolWorker(
            r=r,
            concurrency=args.concurrency,
            target_per_day=args.target_per_day,
            proxy_manager=proxy_mgr,
            batch_size=args.batch_size,
            flush_interval=args.flush_interval,
            decoupled_ingest=args.decoupled_ingest,
        )
        asyncio.run(worker.run())


if __name__ == "__main__":
    main()
