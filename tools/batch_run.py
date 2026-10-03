#!/usr/bin/env python3
"""批量人物页抓取器：按 chunk 调网关 /v1/scrape_batch，断点续跑。

用法：
  ./.venv/bin/python tools/batch_run.py --file data/urls.txt --limit 8 --chunk 4 --lanes 4
  ./.venv/bin/python tools/batch_run.py --file data/urls.txt --limit 0   # 全量（建议后台跑）

输出：
  data/batch_out/results.jsonl   每行一个 URL 的结果摘要（含 person_id/code/engine/ms）
  data/batch_out/html/<pid>.html 成功页 HTML
  data/batch_out/progress.json   已完成 URL（重跑自动跳过）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_urls(path: Path, limit: int) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            u = line.strip()
            if not u or u.startswith("#") or not u.startswith(("http://", "https://")):
                continue
            if u not in seen:
                seen.add(u)
                urls.append(u)
            if limit and len(urls) >= limit:
                break
    return urls


def pid_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1][:64] or "unknown"


def post_batch(gateway: str, urls: list[str], prefix: str, lanes: int, timeout: int) -> dict:
    payload = json.dumps({
        "urls": urls, "session_prefix": prefix, "lanes": lanes, "html": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        gateway.rstrip("/") + "/v1/scrape_batch",
        data=payload, headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def main() -> int:
    ap = argparse.ArgumentParser(description="批量人物页抓取（断点续跑）")
    ap.add_argument("--file", default="data/urls.txt")
    ap.add_argument("--out", default="data/batch_out")
    ap.add_argument("--gateway", default="http://127.0.0.1:8088")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--chunk", type=int, default=4)
    ap.add_argument("--lanes", type=int, default=4)
    ap.add_argument("--prefix", default="bulk")
    ap.add_argument("--sleep", type=float, default=3.0)
    args = ap.parse_args()

    out = Path(args.out)
    (out / "html").mkdir(parents=True, exist_ok=True)
    progress_f = out / "progress.json"
    results_f = out / "results.jsonl"
    try:
        done = set(json.loads(progress_f.read_text(encoding="utf-8")).get("done", []))
    except Exception:
        # 进度文件损坏：先备份再重跑，不静默丢进度
        try:
            if progress_f.exists() and progress_f.stat().st_size > 0:
                bak = out / f"progress.json.bak-{int(time.time())}"
                progress_f.replace(bak)
                print(f"[BATCH] 进度文件损坏，已备份到 {bak.name} 后重跑", flush=True)
        except Exception:
            pass
        done = set()

    urls = [u for u in load_urls(Path(args.file), args.limit) if u not in done]
    print(f"[BATCH] 待跑 {len(urls)} 个（已跳过 {len(done)}），chunk={args.chunk} lanes={args.lanes}", flush=True)
    ok = fail = 0
    t_all = time.time()
    for ci in range(0, len(urls), args.chunk):
        part = urls[ci:ci + args.chunk]
        # 超时按 chunk 动态算：单 URL 最坏 ~600s（浏览器预算），别整 chunk 被误杀
        batch_timeout = max(1500, args.chunk * 600 + 120)
        try:
            data = post_batch(args.gateway, part, f"{args.prefix}{ci // args.chunk}", args.lanes, timeout=batch_timeout)
        except Exception as exc:
            print(f"[BATCH] chunk{ci // args.chunk} 网关异常: {type(exc).__name__}: {exc}，已跳过本 chunk", flush=True)
            continue
        good_urls = []
        with open(results_f, "a", encoding="utf-8") as rf:
            for it in data.get("items", []):
                url = it.get("url", "")
                code = it.get("code")
                good = code in (200, 404, 410)
                ok += good
                fail += (not good)
                if good:
                    good_urls.append(url)
                if good and it.get("html"):
                    try:
                        (out / "html" / f"{pid_of(url)}.html").write_text(it["html"], encoding="utf-8")
                    except Exception:
                        pass
                rf.write(json.dumps({
                    "url": url, "person_id": pid_of(url), "code": code,
                    "engine": it.get("engine"), "attempts": it.get("attempts"),
                    "ms": it.get("elapsed_ms"), "ips": it.get("ips_tried"),
                    "error": (it.get("error") or "")[:200],
                }, ensure_ascii=False) + "\n")
        # 只有定论的才标 done：失败的下次重跑，不丢数据
        done.update(good_urls)
        # 原子写进度：先写 tmp 再 rename，crash 不留半截文件
        tmp_f = out / f"progress.json.tmp-{os.getpid()}"
        tmp_f.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False), encoding="utf-8")
        tmp_f.replace(progress_f)
        print(f"[BATCH] chunk{ci // args.chunk}: ok={ok} fail={fail} "
              f"chunk_ms={data.get('elapsed_ms')} 已用 {time.time() - t_all:.0f}s", flush=True)
        time.sleep(args.sleep)
    print(f"[BATCH] 完成 ok={ok} fail={fail} 总用时 {time.time() - t_all:.0f}s → {results_f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
