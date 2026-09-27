#!/usr/bin/env python3
"""
TruePeopleSearch 生产级守护进程与自愈监控 (tps_supervisor.py)
功能:
  1. 守护托管核心抓取 Worker 与 Dashboard 控制面板
  2. 进程崩溃异常退出时自动自愈拉起 (带重试冷却与退避)
  3. 异常频繁崩溃时触发 Webhook 告警 (tps_alert)
  4. 支持标准管理命令: start / stop / restart / status
  5. 优雅关机 (SIGINT/SIGTERM 级联通知所有子进程平稳退出，不丢数据)
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TOOLS = ROOT / "tools"
LOGS = ROOT / "data" / "logs"
PID_FILE = ROOT / "data" / "supervisor.pid"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tps_alert

PYTHON = sys.executable
for _candidate in (
    ROOT / ".venv" / "bin" / "python3",
    ROOT / ".venv" / "Scripts" / "python.exe",
):
    if _candidate.exists():
        PYTHON = str(_candidate)
        break


class ProcessSpec:
    def __init__(self, name: str, cmd: list, log_file: Path, max_crashes: int = 5):
        self.name = name
        self.cmd = cmd
        self.log_file = log_file
        self.max_crashes = max_crashes
        self.proc: Optional[subprocess.Popen] = None
        self.crash_count = 0
        self.last_crash_time = 0.0
        self.start_time = 0.0
        self.suspended = False

    def start(self) -> None:
        self.suspended = False
        self.start_time = time.time()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        with open(self.log_file, "a", encoding="utf-8") as out:
            self.proc = subprocess.Popen(
                self.cmd,
                stdout=out,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
                env=env,
            )
        print(f"[SUPERVISOR] 启动服务 [{self.name}] PID={self.proc.pid} 日志={self.log_file.name}")

    def is_alive(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        if not self.is_alive():
            return
        print(f"[SUPERVISOR] 正在平稳终止 [{self.name}] PID={self.proc.pid}...")
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"[SUPERVISOR] [{self.name}] 超时未退出，强制终止...")
            self.proc.kill()
            self.proc.wait()


class Supervisor:
    def __init__(self, concurrency: int = None, with_dashboard: bool = True, with_feeder: bool = True):
        self.concurrency = concurrency or int(os.environ.get("TPS_CONCURRENCY", "32"))
        self.with_dashboard = with_dashboard
        self.with_feeder = with_feeder
        self.stopping = False
        self.specs: Dict[str, ProcessSpec] = {}
        self._init_specs()

    def _init_specs(self) -> None:
        worker_cmd = [
            PYTHON,
            str(SCRIPTS / "distributed_worker.py"),
            "--mode", "worker",
            "--concurrency", str(self.concurrency),
        ]
        self.specs["worker"] = ProcessSpec(
            name="worker",
            cmd=worker_cmd,
            log_file=LOGS / "worker.log",
        )

        if self.with_feeder:
            feeder_cmd = [
                PYTHON,
                str(SCRIPTS / "phone_discover.py"),
                "--start-area", os.environ.get("TPS_START_AREA", "201"),
            ]
            self.specs["phone_feeder"] = ProcessSpec(
                name="phone_feeder",
                cmd=feeder_cmd,
                log_file=LOGS / "phone_feeder.log",
            )

        if self.with_dashboard:
            dashboard_cmd = [
                PYTHON,
                str(TOOLS / "dashboard_api.py"),
                "--host", os.environ.get("TPS_DASHBOARD_HOST", "127.0.0.1"),
                "--port", os.environ.get("TPS_DASHBOARD_PORT", "5001"),
            ]
            self.specs["dashboard"] = ProcessSpec(
                name="dashboard",
                cmd=dashboard_cmd,
                log_file=LOGS / "dashboard.log",
            )

    def _on_signal(self, signum, frame):
        print(f"\n[SUPERVISOR] 收到终止信号 ({signum})，开始优雅关停托管服务...")
        self.stopping = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

        print("=" * 60)
        print("  TruePeopleSearch 生产级守护进程与自愈管理器 (Supervisor)")
        print(f"  • PID: {os.getpid()}")
        print(f"  • 抓取并发: {self.concurrency}")
        print(f"  • 守护服务: {', '.join(self.specs.keys())}")
        print("=" * 60)

        for spec in self.specs.values():
            spec.start()

        try:
            while not self.stopping:
                time.sleep(2)
                if self.stopping:
                    break
                now = time.time()
                for spec in self.specs.values():
                    if spec.suspended:
                        continue
                    if not spec.is_alive():
                        exit_code = spec.proc.poll() if spec.proc else -1
                        uptime = now - spec.start_time
                        print(f"[SUPERVISOR] 警告: 服务 [{spec.name}] 异常退出 (code={exit_code}, 运行={uptime:.1f}s)")
                        
                        # 如果运行超过 60 秒才挂掉，重置连续崩溃计数器
                        if uptime > 60:
                            spec.crash_count = 0

                        spec.crash_count += 1
                        spec.last_crash_time = now

                        if spec.crash_count > spec.max_crashes:
                            spec.suspended = True
                            msg = f"服务 [{spec.name}] 连续崩溃达 {spec.crash_count} 次，触发自愈上限已暂停拉起！"
                            print(f"[SUPERVISOR] 错误: {msg}", file=sys.stderr)
                            tps_alert.send_alert("服务持续崩溃告警", msg, level="CRITICAL")
                            continue

                        backoff = min(spec.crash_count * 3, 30)
                        print(f"[SUPERVISOR] 正在触发自愈自启 [{spec.name}] (退避等待 {backoff}s)...")
                        time.sleep(backoff)
                        spec.start()
        finally:
            print("[SUPERVISOR] 正在停止所有子服务...")
            for spec in self.specs.values():
                spec.stop()
            if PID_FILE.exists():
                try:
                    PID_FILE.unlink()
                except Exception:
                    pass
            print("[SUPERVISOR] 守护进程已安全退出。")


def cmd_status() -> None:
    if not PID_FILE.exists():
        print("Supervisor 状态: 未运行 (未发现 PID 文件)")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        print(f"Supervisor 状态: 运行中 (PID={pid})")
    except (ValueError, OSError):
        print("Supervisor 状态: 未运行 (存在残留 PID 文件)")


def cmd_stop() -> None:
    if not PID_FILE.exists():
        print("Supervisor 未在运行。")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        print(f"正在停止 Supervisor (PID={pid})...")
        sig_term = getattr(signal, "SIGTERM", 15)
        sig_kill = getattr(signal, "SIGKILL", sig_term)
        os.kill(pid, sig_term)
        for _ in range(15):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                print("Supervisor 已成功停止。")
                return
        print("停止超时，发送强退信号...")
        os.kill(pid, sig_kill)
    except Exception as exc:
        print(f"停止 Supervisor 失败: {exc}")


def main():
    parser = argparse.ArgumentParser(description="TPS 生产级守护进程与自愈管理器")
    parser.add_argument("action", choices=["start", "stop", "status", "restart"], nargs="?", default="start")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("TPS_CONCURRENCY", "32")), help="Worker 抓取并发数 (默认 32)")
    parser.add_argument("--no-dashboard", action="store_true", help="不守护 Dashboard，仅守护 Worker")
    parser.add_argument("--no-feeder", action="store_true", help="不守护电话号码自动发生器")
    args = parser.parse_args()

    if args.action == "status":
        cmd_status()
    elif args.action == "stop":
        cmd_stop()
    elif args.action == "restart":
        cmd_stop()
        time.sleep(1)
        Supervisor(concurrency=args.concurrency, with_dashboard=not args.no_dashboard, with_feeder=not args.no_feeder).run()
    else:
        Supervisor(concurrency=args.concurrency, with_dashboard=not args.no_dashboard, with_feeder=not args.no_feeder).run()


if __name__ == "__main__":
    main()
