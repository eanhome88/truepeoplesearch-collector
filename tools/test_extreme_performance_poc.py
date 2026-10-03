#!/usr/bin/env python3
"""
极致单机性能验证脚本 (Extreme Single-Machine Performance Engine)
----------------------------------------------------------------
架构特点：
1. 异步 event loop 加速 (uvloop 自动适配)
2. curl_cffi.requests.AsyncSession 全异步连接池复用
3. 生产者-消费者 Async Queue 协程并发管道
4. 精准 Token Bucket 流量与 QPS 控制器
5. 实时性能监控 (QPS、成功率、响应延迟 P95 / P99)

用法：
    python tools/test_extreme_performance_poc.py --workers 50 --qps 100 --duration 10
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 尝试加载 C 语言编写的高性能事件循环 uvloop
try:
    import uvloop
    uvloop.install()
    UVLOOP_ACTIVE = True
except ImportError:
    UVLOOP_ACTIVE = False

try:
    from curl_cffi.requests import AsyncSession
except ImportError:
    print("[ERROR] 缺少 curl_cffi 依赖，请运行: pip install curl-cffi")
    sys.exit(1)


TEST_TARGETS = [
    "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60",
    "https://www.truepeoplesearch.com/find/person/pru8n90r08l08208280u",
]


class RateLimiter:
    """锁式令牌桶控速器，平滑控制吞吐率"""

    def __init__(self, target_qps: int):
        self.target_qps = target_qps
        self.interval = 1.0 / max(1, target_qps) if target_qps > 0 else 0
        self._lock = asyncio.Lock()
        self.last_check = time.monotonic()

    async def acquire(self):
        if self.target_qps <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_check
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self.last_check = time.monotonic()


class PerformanceMetrics:
    """实时统计 QPS、延迟分位数与状态分布"""

    def __init__(self):
        self.total_requests = 0
        self.success = 0
        self.blocked = 0
        self.errors = 0
        self.latencies = []
        self._lock = asyncio.Lock()

    async def record(self, success: bool, blocked: bool, latency: float):
        async with self._lock:
            self.total_requests += 1
            if blocked:
                self.blocked += 1
            elif success:
                self.success += 1
            else:
                self.errors += 1
            self.latencies.append(latency)

    def summary(self, duration_sec: float) -> str:
        qps = self.total_requests / max(0.001, duration_sec)
        succ_rate = (self.success / max(1, self.total_requests)) * 100
        
        if self.latencies:
            sorted_lat = sorted(self.latencies)
            p50 = sorted_lat[int(len(sorted_lat) * 0.50)]
            p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
            avg_lat = sum(sorted_lat) / len(sorted_lat)
        else:
            p50 = p95 = avg_lat = 0.0

        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  🚀 单机极限压测结果汇总 (uvloop: {UVLOOP_ACTIVE})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  • 运行总时长    : {duration_sec:.2f} 秒\n"
            f"  • 请求总数      : {self.total_requests} 次\n"
            f"  • 实测 QPS      : {qps:.1f} 请求/秒\n"
            f"  • 成功率        : {succ_rate:.2f}%\n"
            f"  • 成功 / 风控 / 异常 : {self.success} / {self.blocked} / {self.errors}\n"
            f"  • 响应延迟 P50  : {p50*1000:.1f} ms\n"
            f"  • 响应延迟 P95  : {p95*1000:.1f} ms\n"
            f"  • 平均响应耗时  : {avg_lat*1000:.1f} ms\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )


async def worker_task(
    worker_id: int,
    queue: asyncio.Queue,
    session: AsyncSession,
    rate_limiter: RateLimiter,
    metrics: PerformanceMetrics,
    headers: dict,
):
    while True:
        try:
            url = await queue.get()
        except asyncio.CancelledError:
            break

        await rate_limiter.acquire()
        t0 = time.monotonic()

        try:
            resp = await session.get(url, headers=headers, timeout=12)
            latency = time.monotonic() - t0
            body_text = resp.text

            is_blocked = (
                resp.status_code != 200
                or "internalcaptcha" in body_text.lower()
                or "cf-turnstile" in body_text.lower()
                or "just a moment" in body_text.lower()
            )

            is_success = resp.status_code == 200 and not is_blocked
            await metrics.record(success=is_success, blocked=is_blocked, latency=latency)

        except Exception:
            latency = time.monotonic() - t0
            await metrics.record(success=False, blocked=False, latency=latency)
        finally:
            queue.task_done()


async def live_progress_ticker(metrics: PerformanceMetrics, duration_sec: int):
    """实时每秒打点打出压测进度，不卡死界面"""
    start_t = time.monotonic()
    for sec in range(1, duration_sec + 1):
        await asyncio.sleep(1.0)
        curr_t = time.monotonic() - start_t
        qps = metrics.total_requests / max(0.1, curr_t)
        print(f"[{curr_t:4.1f}s / {duration_sec}s] 已发包: {metrics.total_requests:4d} 次 | 实时 QPS: {qps:5.1f} | 成功: {metrics.success:4d} | 拦截: {metrics.blocked:3d} | 异常: {metrics.errors:3d}", flush=True)


async def run_extreme_engine(
    workers_cnt: int,
    target_qps: int,
    duration_sec: int,
    proxy_url: str,
):
    print("=" * 70)
    print("【单机极限性能引擎】异步 High-QPS 压测模式")
    print(f"• uvloop C-加速    : {'已启用 (Fastest)' if UVLOOP_ACTIVE else '未安装 (使用标准 asyncio)'}")
    print(f"• 并发 Workers    : {workers_cnt} 协程")
    print(f"• 目标 QPS 限制   : {target_qps if target_qps > 0 else '无限制 (全速发包)'}")
    print(f"• 代理地址        : {proxy_url or '直连模式'}")
    print(f"• 测试时长        : {duration_sec} 秒")
    print("=" * 70)
    print("\n[引擎启动] 正在进行连接池预热与并发发包...", flush=True)

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    queue = asyncio.Queue(maxsize=10000)
    rate_limiter = RateLimiter(target_qps)
    metrics = PerformanceMetrics()

    # 启动实时打点器
    ticker_task = asyncio.create_task(live_progress_ticker(metrics, duration_sec))

    async with AsyncSession(impersonate="chrome124", proxies=proxies, max_clients=workers_cnt * 2) as session:
        workers = [
            asyncio.create_task(worker_task(i, queue, session, rate_limiter, metrics, headers))
            for i in range(workers_cnt)
        ]

        start_time = time.monotonic()
        end_time = start_time + duration_sec

        req_idx = 0
        while time.monotonic() < end_time:
            url = TEST_TARGETS[req_idx % len(TEST_TARGETS)]
            req_idx += 1
            await queue.put(url)
            if queue.qsize() > 200:
                await asyncio.sleep(0.01)

        await ticker_task
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        total_elapsed = time.monotonic() - start_time
        print("\n" + metrics.summary(total_elapsed))


def main():
    parser = argparse.ArgumentParser(description="单机极限性能验证与高并发压测引擎")
    parser.add_argument("--workers", type=int, default=20, help="并发 Worker 协程数 (默认: 20)")
    parser.add_argument("--qps", type=int, default=50, help="目标 QPS 限制 (0 表示全速, 默认: 50)")
    parser.add_argument("--duration", type=int, default=5, help="测试持续时间(秒, 默认: 5)")
    parser.add_argument("--proxy", default=os.environ.get("PROXY_TUNNEL"), help="代理地址")
    args = parser.parse_args()

    asyncio.run(run_extreme_engine(args.workers, args.qps, args.duration, args.proxy))


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
