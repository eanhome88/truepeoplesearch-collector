#!/usr/bin/env python3
"""纯协议高速分类：10k URL 只分三类，不进浏览器、不写库。

  dead      协议直接 404/410（真死，3 秒判完）
  unknown   协议被验证/拦截（需浏览器复核）
  alive     协议直接 200 人物页（极少）

用法：
  ./.venv/bin/python tools/sweep_classify.py --file data/urls.txt --out /tmp/sweep.jsonl --concurrency 10
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))
try:
    from tps_env import load_project_env
    load_project_env(ROOT)
except Exception:
    pass

from curl_cffi.requests import AsyncSession
from tps_poc_common import sticky_proxy_url


def _default_proxy() -> str:
    return (os.environ.get("CLOUDBYPASS_PROXY") or os.environ.get("PROXY_TUNNEL") or "").strip()


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": UA,
    "referer": "https://www.google.com/",
}
MARKERS = ("internalcaptcha", "just a moment", "cf-turnstile", "cf-challenge",
           "attention required", "checking your browser",
           "captcha challenge", "are you a human", "verify you are human")
CHALLENGE_TITLE = "captcha challenge"
PERSON = ("phone numbers", "current address")

# verdict 缓存 TTL：死/活定论记 24h；验证 1h（会翻）；限流 5 分钟（波次级）；异常不定论不记。
VERDICT_TTL = {"dead": 86400, "alive": 86400, "challenge": 3600, "throttled": 300}


def _verdict_key(url: str) -> str:
    return "tps:verdict:" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:20]


_VERDICT_REDIS_CLIENT = None


def _verdict_redis():
    """模块级单例：10k URL 共用一个连接，扫完不建 2 万个连接。"""
    global _VERDICT_REDIS_CLIENT
    if _VERDICT_REDIS_CLIENT is not None:
        try:
            _VERDICT_REDIS_CLIENT.ping()
            return _VERDICT_REDIS_CLIENT
        except Exception:
            _VERDICT_REDIS_CLIENT = None
    try:
        import redis  # type: ignore

        client = redis.Redis(
            host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=0.4,
            socket_timeout=0.8,
        )
        client.ping()
        _VERDICT_REDIS_CLIENT = client
        return client
    except Exception:
        return None


def get_verdict(url: str):
    """跨任务 verdict 缓存：扫过的 URL 直接复用定论，不花请求。无则返回 None。"""
    try:
        r = _verdict_redis()
        if r is None:
            return None
        raw = r.get(_verdict_key(url))
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or "cls" not in data:
            return None
        return data
    except Exception:
        return None


def set_verdict(url: str, cls: str, http: int) -> None:
    ttl = VERDICT_TTL.get(cls or "")
    if not ttl:
        return
    try:
        r = _verdict_redis()
        if r is None:
            return
        r.set(_verdict_key(url),
              json.dumps({"cls": cls, "http": http, "ts": time.time()}, ensure_ascii=False),
              ex=ttl)
    except Exception:
        pass


def classify(status: int, text: str) -> str:
    """dead=死链丢弃；alive=协议直出人物页；challenge=验证页走浏览器；
    throttled=429稍后换IP重试；unknown=其他待复核。"""
    if status in (404, 410):
        return "dead"
    if status == 429:
        return "throttled"
    low = (text or "").lower()
    if any(m in low for m in MARKERS):
        return "challenge"
    if status == 403:
        return "challenge"
    if status == 200 and len(text or "") > 10000 and any(p in low for p in PERSON):
        return "alive"
    if status == 200 and len(text or "") >= 500:
        return "unknown"
    # 到这里的都是没法定生死的（短 200、各色 5xx）：一律 unknown，
    # 绝不能判 dead（dead 会进 24h 缓存，真活页就长期丢了）
    return "unknown"


class Polite:
    """按出口限速：同一 sticky 两次请求至少间隔 min_interval 秒。
    全局共享退避门：任一 worker 撞 429/超时/TLS 异常就立起冷却闸，
    所有 worker 一起等波次过去，而不是各睡各的继续撞墙。
    另带全局 rps 上限（跑量时保护对端，100 万/天 ≈ 11.6 rps）。"""

    def __init__(self, min_interval: float = 1.0, backoff_sec: float = 20.0,
                 max_rps: float = 0.0, sick_sec: float = 60.0):
        self.min_interval = max(0.1, min_interval)
        self.backoff_sec = backoff_sec
        self.sick_sec = sick_sec
        self.max_rps = max(0.0, max_rps)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._global_lock = asyncio.Lock()
        self._global_last = 0.0
        self._gate_lock = asyncio.Lock()
        self._cool_until = 0.0
        self.throttles = 0
        self.sick_events = 0

    async def wait(self, proxy: str) -> None:
        if self.max_rps > 0:
            gap = 1.0 / self.max_rps
            async with self._global_lock:
                dt = time.monotonic() - self._global_last
                if dt < gap:
                    await asyncio.sleep(gap - dt)
                self._global_last = time.monotonic()
        # 先过全局退避闸：波次没过去就一起等
        async with self._gate_lock:
            wait_sec = self._cool_until - time.monotonic()
        if wait_sec > 0:
            await asyncio.sleep(wait_sec)
        lock = self._locks.setdefault(proxy, asyncio.Lock())
        async with lock:
            dt = time.monotonic() - self._last.get(proxy, 0.0)
            if dt < self.min_interval:
                await asyncio.sleep(self.min_interval - dt)
            self._last[proxy] = time.monotonic()

    async def _raise_gate(self, secs: float) -> None:
        async with self._gate_lock:
            self._cool_until = max(self._cool_until, time.monotonic() + secs)

    async def on_429(self) -> None:
        self.throttles += 1
        await self._raise_gate(self.backoff_sec)

    async def on_sick(self) -> None:
        """超时/TLS 握手失败：比 429 更重的信号，闸立更久。"""
        self.sick_events += 1
        await self._raise_gate(self.sick_sec)


SAMPLE_NON200 = 32768
SAMPLE_200 = 102400
EARLY_EXIT_AT = 8192


class LanePool:
    """智能 lane 池：坏 lane 自动冷却、好 lane 多分活、sticky 快过期提前换。
    规则：
    - dead/alive/challenge（拿到 verdict）= lane 能用，坏分清零；
    - throttled/异常 = 坏分 +1，冷却 60s×坏分（上限 600s），期间不分活；
    - 取 lane 时只看没在冷却的，坏分最低者优先（平局轮转）；
    - lane 年龄超 25 分钟提前换新 sid，不等 30 分钟过期雪崩；
    - 整组污染（熔断）时换代：全部新 sid，坏分保留一半（出口可能同段）。"""

    def __init__(self, raw: str, n: int, no_sticky: bool = False,
                 lane_ttl: float = 1500.0, cool_base: float = 60.0):
        self.raw = raw
        self.n = max(1, n)
        self.no_sticky = no_sticky or not raw
        self.lane_ttl = lane_ttl
        self.cool_base = cool_base
        self.gen = 0
        self._rr = 0
        self._lock = asyncio.Lock()
        self.lanes = [self._fresh(i) for i in range(self.n)]

    def _sid(self, idx: int, ver: int) -> str:
        suffix = "" if self.gen == 0 and ver == 0 else f"g{self.gen}v{ver}"
        return f"sweep{idx}{suffix}"[:12]

    def _fresh(self, idx: int) -> dict:
        now = time.monotonic()
        if self.no_sticky:
            return {"idx": idx, "sid": "fresh", "proxy": self.raw, "name": "fresh",
                    "born": now, "ver": 0, "ok": 0, "bad": 0, "cool_until": 0.0}
        sid = self._sid(idx, 0)
        return {"idx": idx, "sid": sid, "proxy": sticky_proxy_url(self.raw, sid, 30),
                "name": f"{sid}g{self.gen}", "born": now, "ver": 0,
                "ok": 0, "bad": 0, "cool_until": 0.0}

    def _regen(self, lane: dict) -> None:
        lane["ver"] += 1
        lane["born"] = time.monotonic()
        lane["cool_until"] = 0.0
        if self.no_sticky:
            return
        lane["sid"] = self._sid(lane["idx"], lane["ver"])
        lane["proxy"] = sticky_proxy_url(self.raw, lane["sid"], 30)
        lane["name"] = f"{lane['sid']}g{self.gen}"

    async def pick(self) -> tuple:
        """返回 (idx, proxy, name)。全组冷却中则等最早解封的那条。"""
        while True:
            async with self._lock:
                now = time.monotonic()
                for lane in self.lanes:
                    if now - lane["born"] >= self.lane_ttl:
                        self._regen(lane)
                avail = [l for l in self.lanes if l["cool_until"] <= now]
                if avail:
                    best_bad = min(l["bad"] for l in avail)
                    cands = [l for l in avail if l["bad"] == best_bad]
                    lane = cands[self._rr % len(cands)]
                    self._rr += 1
                    return lane["idx"], lane["proxy"], lane["name"]
                wake = min(l["cool_until"] for l in self.lanes) - now
            await asyncio.sleep(max(0.1, min(wake, 5.0)))

    async def report(self, idx: int, cls: str, err: str = "") -> None:
        async with self._lock:
            if 0 <= idx < len(self.lanes):
                lane = self.lanes[idx]
                if cls in ("dead", "alive", "challenge"):
                    lane["ok"] += 1
                    lane["bad"] = 0
                    lane["cool_until"] = 0.0
                else:
                    lane["bad"] += 1
                    lane["cool_until"] = time.monotonic() + min(
                        self.cool_base * lane["bad"], 600.0)

    async def new_generation(self) -> None:
        """熔断换代：整组新 sid，坏分减半（新出口可能还在同段）。"""
        async with self._lock:
            self.gen += 1
            for lane in self.lanes:
                keep = lane["bad"] // 2
                fresh = self._fresh(lane["idx"])
                fresh["bad"] = keep
                if keep:
                    fresh["cool_until"] = time.monotonic() + self.cool_base * keep
                self.lanes[lane["idx"]] = fresh

    def snapshot(self) -> dict:
        return {l["name"]: {"ok": l["ok"], "bad": l["bad"]} for l in self.lanes}


async def one(sem: asyncio.Semaphore, polite: Polite, session: AsyncSession,
           proxy: str, url: str, lane: str = "", use_cache: bool = True) -> dict:
    # 跨任务缓存先行：命中则零请求零等待，连闸都不用过
    if use_cache:
        hit = get_verdict(url)
        if hit:
            async with sem:
                return {"url": url, "cls": hit.get("cls"), "http": hit.get("http", 0),
                        "ms": 0, "len": 0, "truncated": False, "body_skipped": True,
                        "lane": lane, "cached": True}
    async with sem:
        await polite.wait(proxy)
        t0 = time.time()
        resp = None
        try:
            # 流式只下分类所需的头部字节：403 验证页 518KB 只读前 32KB（省 94% 流量），
            # 429 只看状态码，200 活页判定最多读 100KB（phone/address 标记都在前部）。
            resp = await session.get(url, headers=HEADERS, proxy=proxy, timeout=12, stream=True)
            try:
                status = resp.status_code
                if status == 429:
                    await polite.on_429()
                # 状态码已定生死的（404死/429限流/403验证），一字节正文都不下，直接掐流
                if status in (404, 410, 429, 403):
                    cls = classify(status, "")
                    set_verdict(url, cls, status)
                    return {"url": url, "cls": cls,
                            "http": status, "ms": round((time.time() - t0) * 1000),
                            "len": 0, "truncated": False, "body_skipped": True, "lane": lane}
                cap = SAMPLE_200 if status == 200 else SAMPLE_NON200
                chunks: list = []
                total = 0
                try:
                    async for chunk in resp.aiter_content():
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= cap:
                            break
                        # 非 200 页一旦嗅到验证标记就掐断，不再为大页面烧流量
                        if status != 200 and total >= EARLY_EXIT_AT:
                            probe = b"".join(chunks).decode("utf-8", "ignore").lower()
                            if any(m in probe for m in MARKERS):
                                break
                finally:
                    await resp.aclose()
                text = b"".join(chunks).decode("utf-8", errors="replace")
                cls = classify(status, text)
                set_verdict(url, cls, status)
                return {"url": url, "cls": cls,
                        "http": status, "ms": round((time.time() - t0) * 1000),
                        "len": total, "truncated": total >= cap, "lane": lane}
            finally:
                if resp is not None:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
        except Exception as exc:
            # tarpit 识别：建连后半天不回包是目标站的拖延战术，立重闸；
            # TLS 握手失败同理。别傻撞，等波次过去。
            ename = type(exc).__name__.lower()
            emsg = str(exc).lower()
            if "timeout" in ename or "timeout" in emsg or "timed out" in emsg:
                try:
                    await polite.on_sick()
                except Exception:
                    pass
            elif "ssl" in ename or "ssl" in emsg or "tls" in emsg:
                try:
                    await polite.on_sick()
                except Exception:
                    pass
            return {"url": url, "cls": "unknown", "http": 0,
                    "ms": round((time.time() - t0) * 1000), "err": type(exc).__name__,
                    "lane": lane}


async def main_async(args) -> int:
    urls = [l.strip() for l in open(args.file, encoding="utf-8")
            if l.strip().startswith("http")]
    if args.limit:
        urls = urls[:args.limit]
    # 断点续跑：已扫过的 URL 跳过，out 追加写，不丢历史成果
    done = set()
    if getattr(args, "resume", False):
        try:
            with open(args.out, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        done.add(json.loads(line).get("url", ""))
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
        if done:
            urls = [u for u in urls if u not in done]
            print(f"[SWEEP] 续跑：跳过已扫 {len(done)}，剩余 {len(urls)}", flush=True)
    raw = _default_proxy()
    sem = asyncio.Semaphore(args.concurrency)
    polite = Polite(min_interval=args.interval, backoff_sec=args.backoff, max_rps=args.rps)
    counts = {"dead": 0, "unknown": 0, "alive": 0, "challenge": 0, "throttled": 0}
    pool = LanePool(raw, args.stickies, no_sticky=args.no_sticky)
    print(f"[SWEEP] {len(urls)} urls, concurrency={args.concurrency}, "
          f"stickies={'fresh-every-request' if args.no_sticky else args.stickies}, rps_cap={args.rps or 'none'}",
          flush=True)
    t0 = time.time()
    consec_bad = 0
    # 续跑用追加模式，全新跑用覆盖模式
    fmode = "a" if getattr(args, "resume", False) else "w"
    async with AsyncSession(impersonate="chrome124") as session:
        with open(args.out, fmode, encoding="utf-8") as fh:
            for i in range(0, len(urls), 200):
                batch = urls[i:i + 200]
                # 智能取 lane：坏 lane 冷却中自动跳过，好 lane 多分活
                tasks = []
                picks = [await pool.pick() for u in batch]
                use_cache = not getattr(args, "no_cache", False)
                for (idx, px, lane), u in zip(picks, batch):
                    tasks.append(asyncio.ensure_future(one(sem, polite, session, px, u, lane, use_cache)))
                lane_of = {u: idx for (idx, _, _), u in zip(picks, batch)}
                n_batch = 0
                # as_completed：每完成一个就落盘+打印，不用等整批 200 凑齐才看得见
                for coro in asyncio.as_completed(tasks):
                    r = await coro
                    n_batch += 1
                    counts[r["cls"]] = counts.get(r["cls"], 0) + 1
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    fh.flush()
                    # 缓存命中的没发请求，不参与 lane 健康记分
                    if not r.get("cached"):
                        await pool.report(lane_of.get(r["url"], -1), r["cls"], r.get("err", ""))
                    # 连续无定论（限流/异常）= 波次正紧：停 5 分钟 + 全组换出口，不烧钱空转
                    if r["cls"] in ("throttled",) or (r["cls"] == "unknown" and r.get("err")):
                        consec_bad += 1
                    else:
                        consec_bad = 0
                    if consec_bad >= 60:
                        await pool.new_generation()
                        print(f"[SWEEP] 熔断：连续 60 个无定论，暂停 300s 后换整组出口 (gen{pool.gen})",
                              flush=True)
                        fh.flush()
                        await asyncio.sleep(300)
                        consec_bad = 0
                    if n_batch % 50 == 0:
                        fh.flush()
                        print(f"[SWEEP] batch+{n_batch}/200 {counts} "
                              f"throttles={polite.throttles} sick={polite.sick_events}", flush=True)
                fh.flush()
                print(f"[SWEEP] {min(i + 200, len(urls))}/{len(urls)} {counts} "
                      f"throttles={polite.throttles} sick={polite.sick_events} {time.time() - t0:.0f}s", flush=True)
    print(f"[SWEEP] DONE {counts} in {time.time() - t0:.0f}s → {args.out}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="data/urls.txt")
    ap.add_argument("--out", default="/tmp/sweep.jsonl")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--stickies", type=int, default=5)
    ap.add_argument("--interval", type=float, default=1.0,
                    help="同一出口两次请求最小间隔秒数")
    ap.add_argument("--backoff", type=float, default=20.0,
                    help="遇到 429 后的退避秒数")
    ap.add_argument("--no-sticky", action="store_true",
                    help="跑量模式：不用粘性，每次请求换新出口")
    ap.add_argument("--rps", type=float, default=0.0,
                    help="全局 rps 上限（0=不限，100万/天≈11.6）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：跳过 out 文件里已扫 URL，追加写")
    ap.add_argument("--no-cache", action="store_true",
                    help="不用 verdict 缓存，强制重扫（默认跨任务复用定论）")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
