# -*- coding: utf-8 -*-
"""
TruePeopleSearch 全美电话顺序反查发生器 (Phone Feeder)
标准接入 tps_queue 协议规范，生成标准 Job 注入 Redis，驱动 32 路 Worker 全速消费
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
SCRIPTS = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from tps_env import load_project_env
load_project_env(Path(ROOT), customer_safe=False)

import redis
from phone_plan import (
    HOT_NPAS,
    blocked_flags,
    connect_redis,
    mark_hot_done,
    next_hot_npa,
    note_phone_lookup,
    phone_digits_from_url,
    phone_url,
    prefix_is_cold,
    select_batch,
)
from tps_queue import PENDING_KEY, SEEN_KEY, _dumps, _job_key, new_job

CURSOR_KEY = "tps:phone:cursor"
PAUSE_FILE = Path(ROOT) / "data" / "client.pause"

def get_cursor(r: redis.Redis, default_area: int = 201) -> tuple:
    val = r.get(CURSOR_KEY)
    if val:
        try:
            parts = str(val).split("-")
            return int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            pass
    return default_area, 200, 1

def save_cursor(r: redis.Redis, npa: int, nxx: int, line: int):
    r.set(CURSOR_KEY, f"{npa:03d}-{nxx:03d}-{line:04d}")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def run_feeder(start_area: int = 201, target_queue_size: int = 8000, batch_step: int = 800,
               hot: bool = False):
    r = connect_redis()
    print("=" * 65)
    print("   [START] 美国号码发生器已上线")
    print(f"   * 从区号 {start_area} 起，跳过免费号、服务号和空号段")
    print("   * 查到人物后，该人的其他号码一并入库，并不再重复查询")
    print("=" * 65, flush=True)

    while True:
        try:
            if PAUSE_FILE.exists():
                print("[PHONE FEEDER] 已暂停，不生成新号码", flush=True)
                time.sleep(2)
                continue
            current_pending = int(r.llen(PENDING_KEY) or 0)
            npa, nxx, line = get_cursor(r, start_area)
            prev_npa = npa

            # 热模式：只走高密度区号，走完一个标一个，走完转全量
            if hot:
                try:
                    done_hot = set(r.smembers("tps:phone:hot:done") or set())
                except Exception:
                    done_hot = set()
                if str(npa) in done_hot or npa not in HOT_NPAS:
                    nxt_hot = next_hot_npa(r, 0)
                    if nxt_hot is None:
                        hot = False
                        print("[PHONE FEEDER] 热区号走完，转全量顺序", flush=True)
                    else:
                        npa, nxx, line = nxt_hot, 200, 1
                        save_cursor(r, npa, nxx, line)

            if current_pending < target_queue_size:
                cold_cache = {}

                def cold(area, office):
                    key = (area, office)
                    if key not in cold_cache:
                        cold_cache[key] = prefix_is_cold(r, area, office)
                    return cold_cache[key]

                numbers, nxt = select_batch(
                    npa,
                    nxx,
                    line,
                    batch_step,
                    blocked=lambda batch: blocked_flags(r, batch, SEEN_KEY),
                    cold=cold,
                )
                if numbers:
                    from tps_queue import feed as _feed

                    res = _feed(r, [phone_url(d) for d in numbers])
                    print(
                        f"[PHONE FEEDER] 注入 {res.get('enqueued', 0)} 个号码查询 "
                        f"(排重 {res.get('deduped', 0)} / 无效 {res.get('invalid', 0)}) | "
                        f"待抓池: {int(r.llen(PENDING_KEY) or 0):,} 条 | "
                        f"进度: {npa:03d}-{nxx:03d}-{line:04d}",
                        flush=True,
                    )
                if nxt is None:
                    print("[PHONE FEEDER] 美国可分配号段已走完。", flush=True)
                    time.sleep(30)
                    continue
                npa, nxx, line = nxt
                # 跨区号：把走完的热区号标完工（冷跳整段搬家也算：被跳过的段已有定论）
                if nxt[0] != prev_npa:
                    try:
                        if prev_npa in HOT_NPAS:
                            mark_hot_done(r, prev_npa)
                    except Exception:
                        pass
                save_cursor(r, npa, nxx, line)
                current_pending = int(r.llen(PENDING_KEY) or 0)
                print(
                    f"[PHONE FEEDER] 待抓池: {current_pending:,} 条 | "
                    f"进度: {npa:03d}-{nxx:03d}-{line:04d}",
                    flush=True,
                )
            else:
                print(
                    f"[PHONE FEEDER] 待查池充足 ({current_pending:,} 条) | "
                    f"进度: {npa:03d}-{nxx:03d}-{line:04d}",
                    flush=True,
                )

            time.sleep(2.5)
        except KeyboardInterrupt:
            print("\n[STOP] 电话发生器已退出。")
            break
        except Exception as e:
            print(f"[ERROR] 发生器异常: {e}", flush=True)
            time.sleep(3)

PHONE_HIT_MARKERS = ("/find/person/", "phone numbers", "current address")
PHONE_MISS_MARKERS = ("no result", "no match", "not found", "0 results")
PHONE_BLOCK_MARKERS = ("internalcaptcha", "just a moment", "cf-turnstile", "cf-challenge",
                       "captcha challenge", "attention required", "checking your browser")
PERSON_ID_RE = __import__("re").compile(r"/find/person/([A-Za-z0-9_]+)")


async def probe_numbers(numbers, interval: float = 2.5, timeout: int = 15) -> dict:
    """慢速电话反查：每次全新出口（隧道端轮换）+ 节奏间隔，
    命中回写 hits、未中回写 misses（冷段学习），命中页的人物链入待抓池。
    返回 {digits: {"cls": ..., "http": ...}}。"""
    import asyncio as _aio

    from curl_cffi.requests import AsyncSession as _AsyncSession

    raw = (os.environ.get("CLOUDBYPASS_PROXY") or os.environ.get("PROXY_TUNNEL")
           or os.environ.get("TUNNEL_PROXY") or "").strip()
    r = connect_redis()
    try:
        from tps_queue import feed as _feed
    except Exception:
        _feed = None

    def _classify(status: int, low: str) -> str:
        if status == 404:
            return "miss"
        if status == 429:
            return "throttled"
        if any(m in low for m in PHONE_BLOCK_MARKERS):
            return "challenge"
        if status == 200 and any(m in low for m in PHONE_HIT_MARKERS):
            return "HIT"
        if any(m in low for m in PHONE_MISS_MARKERS):
            return "miss"
        return "unknown"

    out = {}
    async with _AsyncSession(impersonate="chrome124") as session:
        for digits in numbers:
            url = phone_url(digits)
            t0 = time.time()
            try:
                # 全新出口：裸隧道 URL 让隧道端每次换 IP，单出口零压力
                resp = await session.get(url, headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": "en-US,en;q=0.9",
                    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                    "referer": "https://www.google.com/",
                }, proxy=(raw or None), timeout=timeout, stream=True)
                try:
                    status = resp.status_code
                    if status in (404, 429):
                        cls = _classify(status, "")
                        html = ""
                    else:
                        chunks, total = [], 0
                        try:
                            async for ch in resp.aiter_content():
                                if ch:
                                    chunks.append(ch)
                                    total += len(ch)
                                if total >= 100000:
                                    break
                        finally:
                            try:
                                await resp.aclose()
                            except Exception:
                                pass
                        html = b"".join(chunks).decode("utf-8", errors="replace")
                        cls = _classify(status, html.lower())
                finally:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
            except Exception as exc:
                cls, status, html = "unknown", 0, ""
                err = type(exc).__name__
            else:
                err = ""
            out[digits] = {"cls": cls, "http": status, "ms": round((time.time() - t0) * 1000)}
            if err:
                out[digits]["err"] = err
            # 回写学习 + 命中链入：只有定论（命中/明确无记录）才记，
            # 验证/限流/异常不记（那是路的问题，不是号的问题，别污染冷段）
            try:
                if cls in ("HIT", "miss"):
                    note_phone_lookup(r, url, cls == "HIT")
                if cls == "HIT" and _feed is not None:
                    ids = list(dict.fromkeys(PERSON_ID_RE.findall(html or "")))[:20]
                    if ids:
                        _feed(r, [f"https://www.truepeoplesearch.com/find/person/{i}" for i in ids])
                        out[digits]["chained"] = len(ids)
            except Exception:
                pass
            await _aio.sleep(max(0.5, interval))
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TPS Phone Sequential Feeder")
    parser.add_argument("--start-area", type=int, default=201, help="起始区号")
    parser.add_argument("--hot", action="store_true", help="热区号优先走完再转全量")
    parser.add_argument("--probe", default="",
                        help="慢速反查逗号分隔号码，如 --probe 9172001000,9172001001")
    parser.add_argument("--interval", type=float, default=2.5, help="反查节奏秒数")
    args = parser.parse_args()
    if args.probe:
        import asyncio as _aio

        numbers = [d for d in (args.probe.replace(" ", "").split(",")) if d]
        result = _aio.run(probe_numbers(numbers, interval=args.interval))
        for digits, info in result.items():
            print(digits, info)
    else:
        run_feeder(start_area=args.start_area, hot=args.hot)
