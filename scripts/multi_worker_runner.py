#!/usr/bin/env python3
"""
3000万级多进程高并发调度器 (Multi-Process Worker Runner)

解决核心问题：
单进程受限于 Python GIL 与单核 CPU 算力（约在 150~200 QPS 遭遇瓶颈）。
本调度器在单机上自动拉起 N 个独立 Python Worker 进程（充分利用多核 CPU），
结合解耦式入库守护进程，使单机稳健吃满 350 ~ 500 QPS（即 3000 万条/日）。

使用示例：
  # 代理可由本地 Redis 配置或进程环境提供，避免把凭据放进命令行。
  python3 multi_worker_runner.py --workers 4 --concurrency 100
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

_ROOT = str(Path(__file__).resolve().parent.parent)
_SCRIPTS = str(Path(__file__).resolve().parent)


class MultiWorkerManager:
    def __init__(
        self,
        num_workers: int,
        concurrency_per_worker: int,
        proxy_tunnel: Optional[str] = None,
        proxy_file: Optional[str] = None,
        proxy_api: Optional[str] = None,
        decoupled_ingest: bool = True,
        with_ingester: bool = True,
    ):
        self.num_workers = max(1, int(num_workers))
        self.concurrency = max(10, int(concurrency_per_worker))
        self.proxy_tunnel = proxy_tunnel
        self.proxy_file = proxy_file
        self.proxy_api = proxy_api
        self.decoupled_ingest = decoupled_ingest
        self.with_ingester = with_ingester

        self.processes: List[subprocess.Popen] = []
        self.ingester_process: Optional[subprocess.Popen] = None
        self.stopping = False
        self._term_sent = set()

    def _build_worker_cmd(self, worker_idx: int) -> List[str]:
        cmd = [
            sys.executable,
            os.path.join(_SCRIPTS, "protocol_worker.py"),
            "--mode", "worker",
            "--concurrency", str(self.concurrency),
        ]
        if self.decoupled_ingest:
            cmd.append("--decoupled-ingest")
        return cmd

    def _worker_env(self) -> dict:
        env = os.environ.copy()
        if self.proxy_tunnel:
            env["PROXY_TUNNEL"] = self.proxy_tunnel
        elif self.proxy_file:
            env["PROXY_FILE"] = self.proxy_file
        elif self.proxy_api:
            env["PROXY_API"] = self.proxy_api
        return env

    def _build_ingester_cmd(self) -> List[str]:
        return [
            sys.executable,
            os.path.join(_SCRIPTS, "bulk_ingester_daemon.py"),
            "--batch-size", "1000",
            "--flush-interval", "0.5",
        ]

    def _handle_signal(self, sig, _frame):
        signame = signal.Signals(sig).name
        if self.stopping:
            return
        print(f"\n[MANAGER] 收到 {signame}，正在通知所有工作进程与入库进程平稳停机...")
        self.stopping = True
        self._terminate_all()

    def _terminate_all(self):
        for p in self._managed_processes():
            if p in self._term_sent:
                continue
            self._term_sent.add(p)
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass

    def _kill_all(self):
        for p in self._managed_processes():
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass

    def _managed_processes(self):
        return [*self.processes, *([self.ingester_process] if self.ingester_process else [])]

    def _shutdown_all(self):
        self.stopping = True
        self._terminate_all()
        processes = self._managed_processes()
        print("[MANAGER] 等待子进程安全退出 (最多 15 秒)...")
        deadline = time.monotonic() + 15
        for p in processes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                if p.poll() is None:
                    p.wait(timeout=remaining)
            except Exception:
                pass
        self._kill_all()
        reap_deadline = time.monotonic() + 1
        for p in processes:
            remaining = reap_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                p.wait(timeout=remaining)
            except Exception:
                pass

    def start(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        total_concurrency = self.num_workers * self.concurrency
        print("============================================================")
        print(f"  TruePeopleSearch 3000万级多进程高并发调度中心")
        print("============================================================")
        print(f"  • 工作子进程数: {self.num_workers} 个")
        print(f"  • 单进程协程数: {self.concurrency} 个")
        print(f"  • 全机总连接数: {total_concurrency} 并发连接")
        print(f"  • 目标设计吞吐: 350 ~ 500 QPS (约 3,000 万 ~ 4,300 万条/天)")
        proxy_mode = "tunnel" if self.proxy_tunnel else "file" if self.proxy_file else "api" if self.proxy_api else "local config"
        print(f"  • 代理模式配置: {proxy_mode}")
        print(f"  • 解耦高速入库: {'启用 (Redis Buffer 解耦)' if self.decoupled_ingest else '禁用 (直写 TiDB)'}")
        print("============================================================")

        try:
            # Keep every successful Popen handle in the cleanup path, even if
            # a later launch or restart raises before the monitor loop starts.
            if self.with_ingester and self.decoupled_ingest and not self.stopping:
                print("[MANAGER] 正在启动专职批量入库守护进程...")
                self.ingester_process = subprocess.Popen(
                    self._build_ingester_cmd(),
                    stdin=subprocess.DEVNULL,
                )

            for i in range(self.num_workers):
                if self.stopping:
                    break
                cmd = self._build_worker_cmd(i)
                print(f"[MANAGER] 正在启动抓取 Worker 进程 #{i+1} (并发 {self.concurrency})...")
                p = subprocess.Popen(
                    cmd,
                    env=self._worker_env(),
                    stdin=subprocess.DEVNULL,
                )
                self.processes.append(p)
                if self.stopping:
                    break
                time.sleep(0.3)

            if not self.stopping:
                print("[MANAGER] 所有子进程已就绪，调度中心进入常驻监控循环 (Ctrl+C 优雅停机)...")

            while not self.stopping:
                time.sleep(2)
                # 检查抓取进程
                for i, p in enumerate(self.processes):
                    ret = p.poll()
                    if ret is not None and not self.stopping:
                        print(f"[WARN] 抓取进程 #{i+1} 异常退出 (code: {ret})，正在自动拉起新进程...", file=sys.stderr)
                        new_cmd = self._build_worker_cmd(i)
                        self.processes[i] = subprocess.Popen(
                            new_cmd,
                            env=self._worker_env(),
                            stdin=subprocess.DEVNULL,
                        )

                # 检查入库守护进程
                if self.ingester_process and not self.stopping:
                    ret = self.ingester_process.poll()
                    if ret is not None:
                        print(f"[WARN] 入库守护进程异常退出 (code: {ret})，正在自动重启...", file=sys.stderr)
                        self.ingester_process = subprocess.Popen(
                            self._build_ingester_cmd(),
                            stdin=subprocess.DEVNULL,
                        )

        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self._shutdown_all()
        print("[MANAGER] 所有进程已安全关闭，调度中心退出完成。")


def main():
    cpu_cores = os.cpu_count() or 4
    default_workers = max(2, min(8, cpu_cores))

    parser = argparse.ArgumentParser(description="3000万/天 协议层多进程高并发调度中心")
    parser.add_argument("--workers", type=int, default=default_workers, help=f"工作子进程数量 (默认依据核心数: {default_workers})")
    parser.add_argument("--concurrency", type=int, default=80, help="每个子进程内协程并发数 (默认 80)")
    parser.add_argument("--proxy-file", type=str, default=os.environ.get("PROXY_FILE"), help="本地代理列表文件路径")
    parser.add_argument("--no-decoupled", action="store_true", help="禁用解耦缓冲，直接在 Worker 中批量写入 TiDB")
    parser.add_argument("--no-ingester", action="store_true", help="不自动拉起批量入库守护进程 (需在其他地方单独拉起)")

    for option, environment in (("--proxy-tunnel", "PROXY_TUNNEL"), ("--proxy-api", "PROXY_API")):
        if any(arg == option or arg.startswith(option + "=") for arg in sys.argv[1:]):
            parser.error(f"{option} 已停用；请通过 {environment} 环境变量或本地代理配置提供地址")
    args = parser.parse_args()

    if os.environ.get("TPS_ALLOW_CLUSTER") != "1":
        print(
            "[CLUSTER] refused: set TPS_ALLOW_CLUSTER=1 to start the protocol cluster",
            file=sys.stderr,
        )
        sys.exit(0)

    mgr = MultiWorkerManager(
        num_workers=args.workers,
        concurrency_per_worker=args.concurrency,
        proxy_tunnel=os.environ.get("PROXY_TUNNEL"),
        proxy_file=args.proxy_file,
        proxy_api=os.environ.get("PROXY_API"),
        decoupled_ingest=not args.no_decoupled,
        with_ingester=not args.no_ingester,
    )
    mgr.start()


if __name__ == "__main__":
    main()
