#!/usr/bin/env python3
"""
TruePeopleSearch 生产级守护进程与自愈监控 (tps_supervisor.py)
功能:
  1. 守护托管核心抓取 Worker 与 Dashboard 控制面板
  2. 进程崩溃异常退出时自动自愈拉起 (带重试冷却与退避)
  3. 异常频繁崩溃时触发 Webhook 告警 (tps_alert)
  4. 支持标准管理命令: start / stop / restart / status
  5. 优雅关机 (SIGINT/SIGTERM 级联通知所有子进程平稳退出，不丢数据)
  6. --dashboard-only 显式仅启动回环地址上的 Dashboard，不启动采集服务
"""

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TOOLS = ROOT / "tools"
# 运行时状态禁止写入受验的 app/ 树：pid 进安装根 runtime/，日志进安装根 logs/。
# （客户机上 ROOT.parent 即 D:\truepeoplesearch；tps_control.LOG_DIR 须与此同目录。）
LOGS = ROOT.parent / "logs" / "supervisor"
PID_FILE = ROOT.parent / "runtime" / "supervisor.pid"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tps_env import customer_release_mode, dashboard_port, load_project_env

# A trusted launcher either sets customer mode before this process starts or
# invokes the explicit dashboard-only action.  In either case, do not let an
# editable .env file supply anything beyond the customer-safe local settings.
load_project_env(
    ROOT,
    customer_safe=customer_release_mode(os.environ) or "--dashboard-only" in sys.argv,
)

import tps_alert

PYTHON = sys.executable
if os.name == "nt":
    _candidates = (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python3",
    )
else:
    _candidates = (
        ROOT / ".venv" / "bin" / "python3",
        ROOT / ".venv" / "Scripts" / "python.exe",
    )
for _candidate in _candidates:
    if _candidate.exists():
        PYTHON = str(_candidate)
        break


class SupervisorAlreadyRunning(RuntimeError):
    """A different supervisor owns this installation's process record."""


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and "python" in p.name().lower()
    except Exception:
        pass
    if os.name == "nt":
        # os.kill(pid, 0) is not a portable liveness probe on Windows.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _mode_file(pid_file: Path) -> Path:
    return pid_file.with_name(pid_file.name + ".mode.json")


def _process_start_marker(pid: int) -> Optional[int]:
    """Return a bounded PID-generation marker without trusting a PID alone.

    Windows can reuse a numeric PID after a crashed Supervisor leaves its mode
    file behind. A dashboard-only stop must therefore bind its record to both
    the PID and this process-generation marker before it targets a process
    tree. ``psutil`` is already an approved dashboard dependency; failure to
    inspect a process deliberately yields no marker rather than guessing.
    """
    if pid <= 0:
        return None
    try:
        import psutil

        started_at = psutil.Process(pid).create_time()
        marker = int(float(started_at) * 1000)
    except (ImportError, OSError, ValueError, OverflowError):
        return None
    except Exception:
        # psutil raises platform-specific access/no-such-process subclasses.
        return None
    return marker if marker > 0 else None


def _mode_record_for_process(pid: int, mode: str, *, require_identity: bool) -> dict:
    record = {"pid": pid, "mode": mode}
    marker = _process_start_marker(pid)
    if marker is None:
        if require_identity:
            raise SupervisorAlreadyRunning("无法确认客户控制台进程身份，拒绝启动。")
        return record
    record["process_start_marker"] = marker
    return record


def _write_mode_record(pid_file: Path, mode: str, *, require_identity: bool = False) -> None:
    """Atomically identify the launch mode for safe, scoped stop requests."""
    if mode not in ("dashboard-only", "full"):
        raise ValueError("unsupported supervisor mode")
    mode_path = _mode_file(pid_file)
    mode_path.parent.mkdir(parents=True, exist_ok=True)
    mode_record = _mode_record_for_process(
        os.getpid(), mode, require_identity=require_identity,
    )
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=mode_path.parent,
            prefix=mode_path.name + ".", suffix=".tmp", delete=False,
        ) as record:
            temporary_path = Path(record.name)
            json.dump(mode_record, record, sort_keys=True)
            record.write("\n")
            record.flush()
        os.replace(temporary_path, mode_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_mode_record(pid_file: Path) -> Optional[dict]:
    try:
        data = json.loads(_mode_file(pid_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or type(data.get("pid")) is not int or data.get("pid") <= 0:
        return None
    if data.get("mode") not in ("dashboard-only", "full"):
        return None
    allowed_keys = {"pid", "mode", "process_start_marker"}
    if set(data) - allowed_keys:
        return None
    marker = data.get("process_start_marker")
    if marker is not None and (type(marker) is not int or marker <= 0):
        return None
    return data


def _remove_own_mode_record(pid_file: Path, pid: int, mode: str) -> None:
    mode_path = _mode_file(pid_file)
    record = _read_mode_record(pid_file)
    expected = _mode_record_for_process(pid, mode, require_identity=False)
    if record != expected:
        return
    try:
        mode_path.unlink()
    except OSError:
        pass


class _SupervisorSingleton:
    """Hold an OS lock for the complete startup/shutdown lifecycle.

    The separate lock file must never be unlinked: replacing its inode could let
    a second process lock a different file while the first owner is still alive.
    The PID file remains a plain integer for existing status/stop commands.
    """

    def __init__(self, pid_file: Path):
        self.pid_file = pid_file
        self.lock_file = pid_file.with_name(pid_file.name + ".lock")
        self.pid = os.getpid()
        self._handle = None
        self._locked = False
        self._pid_identity = None

    @staticmethod
    def _identity(stat):
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size

    def __enter__(self):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.lock_file.open("a+b")
        try:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0, os.SEEK_END)
                    if self._handle.tell() == 0:
                        self._handle.write(b"\0")
                        self._handle.flush()
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise SupervisorAlreadyRunning("Supervisor 已在运行，拒绝重复启动。") from exc
                raise

            # Also protect a live supervisor started by a version without locks.
            try:
                existing_pid = int(self.pid_file.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError, UnicodeDecodeError):
                existing_pid = None
            if existing_pid is not None and _pid_is_running(existing_pid):
                raise SupervisorAlreadyRunning(
                    f"Supervisor PID 文件仍属于运行中的进程 ({existing_pid})，拒绝覆盖。"
                )

            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.pid_file.parent,
                    prefix=self.pid_file.name + ".", suffix=".tmp", delete=False,
                ) as record:
                    temporary_path = Path(record.name)
                    record.write(str(self.pid))
                    record.flush()
                # Windows can finalize modification time only after close.
                identity = self._identity(temporary_path.stat())
                os.replace(temporary_path, self.pid_file)
                self._pid_identity = identity
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._pid_identity is not None:
                try:
                    # Do not remove a PID record rewritten/replaced by anybody else.
                    if (self._identity(self.pid_file.stat()) == self._pid_identity
                            and self.pid_file.read_text(encoding="utf-8").strip() == str(self.pid)):
                        self.pid_file.unlink()
                except (OSError, UnicodeDecodeError):
                    pass
                self._pid_identity = None
        finally:
            if self._handle is not None:
                try:
                    if self._locked:
                        if os.name == "nt":
                            import msvcrt

                            self._handle.seek(0)
                            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                finally:
                    self._handle.close()
                    self._handle = None
                    self._locked = False


class ProcessSpec:
    def __init__(
        self,
        name: str,
        cmd: list,
        log_file: Path,
        max_crashes: int = 5,
        env_overrides: Optional[Dict[str, str]] = None,
    ):
        self.name = name
        self.cmd = cmd
        self.log_file = log_file
        self.max_crashes = max_crashes
        self.env_overrides = dict(env_overrides or {})
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
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if "TPS_DB_PORT" not in env:
            env["TPS_DB_PORT"] = "4000"
        env.update(self.env_overrides)
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
    def __init__(
        self,
        concurrency: int = None,
        with_dashboard: bool = True,
        with_feeder: bool = True,
        with_worker: bool = True,
        dashboard_host: Optional[str] = None,
    ):
        self.concurrency = concurrency or int(os.environ.get("TPS_CONCURRENCY", "32"))
        self.with_dashboard = with_dashboard
        self.with_feeder = with_feeder
        self.with_worker = with_worker
        self.dashboard_host = dashboard_host
        self.dashboard_only = (
            self.with_dashboard and not self.with_feeder and not self.with_worker
            and self.dashboard_host == "127.0.0.1"
        )
        self.customer_release = self.dashboard_only or customer_release_mode(os.environ)
        if self.customer_release and not self.dashboard_only:
            raise ValueError("客户发布模式只能启动本机 Dashboard")
        self.stopping = False
        self.specs: Dict[str, ProcessSpec] = {}
        self._init_specs()

    def _init_specs(self) -> None:
        if self.with_worker:
            worker_mode = os.environ.get("TPS_WORKER_MODE", "browser").lower()
            if worker_mode == "browser" and (SCRIPTS / "distributed_worker.py").exists():
                worker_script = SCRIPTS / "distributed_worker.py"
            else:
                raise ValueError("Supervisor 不再支持独立协议 Worker；请使用带可靠入库守护进程的协议集群")
            worker_cmd = [
                PYTHON,
                str(worker_script),
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
                "--hot",
            ]
            self.specs["phone_feeder"] = ProcessSpec(
                name="phone_feeder",
                cmd=feeder_cmd,
                log_file=LOGS / "phone_feeder.log",
            )

        if self.with_dashboard:
            dashboard_port_value = str(dashboard_port(os.environ))
            dashboard_cmd = [
                PYTHON,
                str(TOOLS / "dashboard_api.py"),
                "--host", self.dashboard_host or os.environ.get("TPS_DASHBOARD_HOST", "127.0.0.1"),
                "--port", dashboard_port_value,
            ]
            if self.dashboard_only:
                # The customer launcher waits for this exact configured port;
                # never silently move it to a nearby free port.
                dashboard_cmd.append("--strict-port")
            self.specs["dashboard"] = ProcessSpec(
                name="dashboard",
                cmd=dashboard_cmd,
                log_file=LOGS / "dashboard.log",
                env_overrides={"TPS_RELEASE_MODE": "customer"} if self.dashboard_only else None,
            )

    def _on_signal(self, signum, frame):
        print(f"\n[SUPERVISOR] 收到终止信号 ({signum})，开始优雅关停托管服务...")
        self.stopping = True

    def run(self) -> None:
        # Acquire ownership before signals, PID writes, or any child starts.
        with _SupervisorSingleton(PID_FILE):
            mode = "dashboard-only" if self.dashboard_only else "full"
            _write_mode_record(PID_FILE, mode, require_identity=self.dashboard_only)
            try:
                self._run_owned()
            finally:
                _remove_own_mode_record(PID_FILE, os.getpid(), mode)

    def _run_owned(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        print("=" * 60)
        print("  TruePeopleSearch 生产级守护进程与自愈管理器 (Supervisor)")
        print(f"  • PID: {os.getpid()}")
        print(f"  • 抓取并发: {self.concurrency}")
        print(f"  • 守护服务: {', '.join(self.specs.keys())}")
        print("=" * 60)

        try:
            for spec in self.specs.values():
                spec.start()

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
                            tps_alert.send_alert(
                                "服务持续崩溃告警",
                                msg,
                                level="CRITICAL",
                                outbound_enabled=not self.customer_release,
                            )
                            continue

                        backoff = min(spec.crash_count * 3, 30)
                        print(f"[SUPERVISOR] 正在触发自愈自启 [{spec.name}] (退避等待 {backoff}s)...")
                        time.sleep(backoff)
                        spec.start()
        finally:
            print("[SUPERVISOR] 正在停止所有子服务...")
            for spec in self.specs.values():
                spec.stop()
            print("[SUPERVISOR] 守护进程已安全退出。")


def cmd_status() -> None:
    if not PID_FILE.exists():
        print("Supervisor 状态: 未运行 (未发现 PID 文件)")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        if _pid_is_running(pid):
            print(f"Supervisor 状态: 运行中 (PID={pid})")
        else:
            print("Supervisor 状态: 未运行 (存在残留 PID 文件)")
    except Exception:
        print("Supervisor 状态: 未运行")


def _dashboard_only_record_matches_process(pid: int, record: Optional[dict]) -> bool:
    if record is None or record.get("pid") != pid or record.get("mode") != "dashboard-only":
        return False
    marker = _process_start_marker(pid)
    return marker is not None and record.get("process_start_marker") == marker


def _stop_windows_dashboard_tree(pid: int) -> bool:
    """Stop only the verified customer dashboard process tree on Windows."""
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def cmd_stop(*, dashboard_only: bool = False) -> bool:
    if not PID_FILE.exists():
        print("Supervisor 未在运行。")
        return True
    try:
        pid = int(PID_FILE.read_text().strip())
        if dashboard_only:
            record = _read_mode_record(PID_FILE)
            if not _dashboard_only_record_matches_process(pid, record):
                print("拒绝停止：未确认本次客户控制台的进程身份。")
                return False
        print(f"正在停止 Supervisor (PID={pid})...")
        if os.name == "nt":
            if not _stop_windows_dashboard_tree(pid):
                print("停止失败：目标进程仍可能运行；保留进程记录以便复核。")
                return False
            try:
                PID_FILE.unlink(missing_ok=True)
                PID_FILE.with_name(PID_FILE.name + ".lock").unlink(missing_ok=True)
            except Exception:
                pass
            print("Supervisor 已成功停止。")
            return True
        sig_term = getattr(signal, "SIGTERM", 15)
        sig_kill = getattr(signal, "SIGKILL", sig_term)
        os.kill(pid, sig_term)
        for _ in range(15):
            time.sleep(1)
            if not _pid_is_running(pid):
                print("Supervisor 已成功停止。")
                return True
        print("停止超时，发送强退信号...")
        os.kill(pid, sig_kill)
        return False
    except Exception as exc:
        print(f"停止 Supervisor 失败: {exc}")
        return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TPS 生产级守护进程与自愈管理器")
    parser.add_argument("action", choices=["start", "stop", "status", "restart"], nargs="?", default="start")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("TPS_CONCURRENCY", "32")), help="Worker 抓取并发数 (默认 32)")
    parser.add_argument("--no-dashboard", action="store_true", help="不守护 Dashboard，仅守护 Worker")
    parser.add_argument("--no-feeder", action="store_true", help="不守护电话号码自动发生器")
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="仅启动本机 Dashboard（不启动 Worker 或电话号码发生器）",
    )
    return parser


def _parse_args(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dashboard_only:
        if args.action not in ("start", "stop"):
            parser.error("--dashboard-only 仅可用于 start 或受限的 stop")
        if args.no_dashboard:
            parser.error("--dashboard-only 不能与 --no-dashboard 同时使用")
    if customer_release_mode(os.environ) and not args.dashboard_only:
        parser.error("客户发布模式只能使用 --dashboard-only 启动本机 Dashboard")
    return args


def _build_supervisor(args) -> Supervisor:
    if args.dashboard_only:
        # Do not inherit an environment override here: this mode is deliberately
        # loopback-only and has no collection subprocesses in its spec set.
        return Supervisor(
            concurrency=args.concurrency,
            with_dashboard=True,
            with_feeder=False,
            with_worker=False,
            dashboard_host="127.0.0.1",
        )
    return Supervisor(
        concurrency=args.concurrency,
        with_dashboard=not args.no_dashboard,
        with_feeder=not args.no_feeder,
    )


def main() -> int:
    args = _parse_args()

    if args.action == "status":
        cmd_status()
    elif args.action == "stop":
        return 0 if cmd_stop(dashboard_only=args.dashboard_only) else 1
    elif args.action == "restart":
        if not cmd_stop():
            return 1
        time.sleep(1)
        _build_supervisor(args).run()
    else:
        _build_supervisor(args).run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SupervisorAlreadyRunning as exc:
        print(f"[SUPERVISOR] {exc}", file=sys.stderr)
        sys.exit(1)
