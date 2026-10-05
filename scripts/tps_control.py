#!/usr/bin/env python3
"""
面板用的抓取进程控制：启停 worker / discover，并汇总心跳与队列进度。

进程状态以 Redis + 本机 PID 为准，Flask 热重载后仍能认出自己拉起的子进程。
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tps_queue import peek_dlq, peek_processing, queue_stats
from tps_scale import MAX_BROWSER_CONCURRENCY
from local_logs import compact_inactive_log, tail_lines

try:
    import psutil
except ImportError:
    psutil = None

LOG_DIR = ROOT.parent / "logs" / "supervisor"
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

CONTROL_WORKER_KEY = "tps:control:worker"
CONTROL_DISCOVER_KEY = "tps:control:discover"
HB_WORKER_PREFIX = "tps:hb:worker:"
HB_DISCOVER_KEY = "tps:hb:discover"
DISCOVER_PENDING = "tps:discover:pending"
DISCOVER_SEEN = "tps:discover:seen"
BULK_COMMITTED_TOTAL_KEY = "tps:ingest:bulk:committed_total"
BULK_RATE_KEY = "tps:ingest:bulk:rate"
BULK_RATE_TTL_SEC = 15

WORKER_HB_TTL = 75
DISCOVER_HB_TTL = 120
STOP_WAIT_SEC = 12
START_LOCK_TTL_SEC = 120
_ACTIVE_START_LOCK = ContextVar("tps_active_start_lock", default=None)
_RENEW_START_LOCK = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) else return 0 end"
_RELEASE_START_LOCK = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
_REGISTER_START = "if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('SET', KEYS[2], ARGV[2]); return 1 else return 0 end"

_WORKER_NEEDLE = "distributed_worker.py"
_DISCOVER_NEEDLE = "discover.py"
_LETTERS_RE = re.compile(
    r"^(all|\*|[a-z]([,-][a-z])*)$",
    re.IGNORECASE,
)


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(_as_str(value)))
    except (TypeError, ValueError):
        return default


def _loads(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = _as_str(raw).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def normalize_letters(spec: str) -> str:
    text = (spec or "a").strip().lower().replace(" ", "")
    if not text:
        text = "a"
    if not _LETTERS_RE.match(text):
        raise ValueError("字母范围只能是 a、a-c、a,c 或 all")
    return "all" if text in {"all", "*"} else text


def write_worker_heartbeat(r, payload: dict, ttl: int = WORKER_HB_TTL) -> None:
    worker_id = _as_str(payload.get("worker_id") or payload.get("pid") or "unknown")
    body = dict(payload)
    body["ts"] = int(time.time())
    r.set(f"{HB_WORKER_PREFIX}{worker_id}", _dumps(body), ex=int(ttl))


def clear_worker_heartbeat(r, worker_id: str) -> None:
    if worker_id:
        r.delete(f"{HB_WORKER_PREFIX}{worker_id}")


def write_discover_heartbeat(r, payload: dict, ttl: int = DISCOVER_HB_TTL) -> None:
    body = dict(payload)
    body["ts"] = int(time.time())
    r.set(HB_DISCOVER_KEY, _dumps(body), ex=int(ttl))


def clear_discover_heartbeat(r) -> None:
    r.delete(HB_DISCOVER_KEY)


def _list_ps() -> List[dict]:
    try:
        out = subprocess.check_output(
            ["ps", "-ax", "-o", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        try:
            pid = int(parts[0])
        except (TypeError, ValueError):
            continue
        rows.append({"pid": pid, "cmd": parts[1] if len(parts) > 1 else ""})
    return rows


def _is_worker_cmd(cmd: str) -> bool:
    if _WORKER_NEEDLE not in cmd:
        return False
    if "--mode" in cmd and not re.search(r"--mode\s+worker\b", cmd):
        return False
    return True


def _is_discover_cmd(cmd: str) -> bool:
    if "test_discover.py" in cmd:
        return False
    return bool(re.search(r"(^|[/\s])discover\.py(\s|$)", cmd))


def _project_script_in_command(cmd: str, script_name: str) -> bool:
    """Recognize only this checkout's absolute script argument."""
    try:
        args = shlex.split(cmd)
    except ValueError:
        return False
    if len(args) < 2:
        return False
    script_index = 2 if len(args) > 1 and args[1] == "-u" else 1
    if len(args) <= script_index:
        return False
    actual = args[script_index]
    target = str(SCRIPTS / script_name)
    if actual.startswith("/"):
        return actual == target
    return actual in (target, f"scripts/{script_name}", script_name, f"./{script_name}", f"./scripts/{script_name}")


def find_role_pids(role: str) -> List[dict]:
    pred = _is_worker_cmd if role == "worker" else _is_discover_cmd
    script = "distributed_worker.py" if role == "worker" else "discover.py"
    found = []
    for row in _list_ps():
        if pred(row["cmd"]) and _project_script_in_command(row["cmd"], script) and pid_alive(row["pid"]):
            found.append(row)
    return found


def _read_control(r, key: str) -> dict:
    return _loads(r.get(key)) or {}


def _write_control(r, key: str, payload: dict) -> None:
    r.set(key, _dumps(payload))


def _serialized_start(control_key: str, *, need_psutil: bool = True):
    """Serialize one role's control operation across dashboard processes."""
    lock_key = f"{control_key}:start-lock"

    def decorate(fn):
        @wraps(fn)
        def wrapped(r, *args, **kwargs):
            if need_psutil and psutil is None:
                raise RuntimeError("缺少 psutil，不能安全管理子进程；请先安装 psutil")
            token = uuid.uuid4().hex
            try:
                acquired = r.set(lock_key, token, nx=True, ex=START_LOCK_TTL_SEC)
            except Exception:
                return {"ok": False, "error": "操作锁不可用，已拒绝执行"}
            if not acquired:
                return {"ok": False, "error": "正在执行控制操作，请稍后重试"}

            stopped = threading.Event()
            lost = threading.Event()

            def renew():
                while not stopped.wait(START_LOCK_TTL_SEC / 3):
                    try:
                        if r.eval(_RENEW_START_LOCK, 1, lock_key, token, START_LOCK_TTL_SEC) != 1:
                            lost.set()
                            return
                    except Exception:
                        lost.set()
                        return

            refresher = threading.Thread(target=renew, daemon=True)
            refresher.start()
            context = _ACTIVE_START_LOCK.set((lock_key, token, lost))
            try:
                return fn(r, *args, **kwargs)
            finally:
                _ACTIVE_START_LOCK.reset(context)
                stopped.set()
                refresher.join(timeout=1)
                try:
                    r.eval(_RELEASE_START_LOCK, 1, lock_key, token)
                except Exception:
                    pass  # Expiry releases a lock when Redis is unavailable.

        return wrapped

    return decorate


def _tail_log(path: Path, n: int = 24) -> List[str]:
    return tail_lines(path, n)


def _pause_from_beats(beats) -> tuple:
    """心跳里 status=paused 时，面板显示还要等多久。"""
    remaining = 0
    for beat in beats or []:
        if not isinstance(beat, dict):
            continue
        if _as_str(beat.get("status")) != "paused":
            continue
        remaining = max(remaining, _as_int(beat.get("pause_remaining_sec")))
    return remaining > 0, remaining


def _scan_heartbeats(r) -> List[dict]:
    out = []
    scan = getattr(r, "scan_iter", None)
    keys = []
    if callable(scan):
        try:
            keys = list(scan(f"{HB_WORKER_PREFIX}*"))
        except TypeError:
            keys = list(scan(match=f"{HB_WORKER_PREFIX}*"))
    else:
        keys_fn = getattr(r, "keys", None)
        if callable(keys_fn):
            keys = list(keys_fn(f"{HB_WORKER_PREFIX}*") or [])
    for key in keys:
        payload = _loads(r.get(key))
        if payload:
            out.append(payload)
    out.sort(key=lambda x: _as_str(x.get("worker_id")))
    return out


def _role_status(r, role: str, *, read_only: bool = False) -> dict:
    """Return a role's state, preserving stale records for read-only callers."""
    key = CONTROL_WORKER_KEY if role == "worker" else CONTROL_DISCOVER_KEY
    saved = _read_control(r, key)
    procs = find_role_pids(role)
    pids = [p["pid"] for p in procs]
    saved_pid = _as_int(saved.get("pid"))
    if saved_pid and saved_pid not in pids and _owned_process(saved, "distributed_worker.py" if role == "worker" else "discover.py"):
        pids.append(saved_pid)
    running = bool(pids)
    if not running and saved and not read_only:
        r.delete(key)
        saved = {}
    status = {
        "running": running,
        "pids": pids,
        "started_at": saved.get("started_at"),
        "args": saved.get("args") or {},
    }
    if role == "worker":
        beats = _scan_heartbeats(r)
        status["heartbeats"] = beats
        conc = 0
        inflight = []
        for beat in beats:
            conc = max(conc, _as_int(beat.get("concurrency")))
            for job in beat.get("inflight") or []:
                if isinstance(job, dict):
                    inflight.append(job)
        if not conc:
            conc = _as_int((saved.get("args") or {}).get("concurrency"), 0)
        status["concurrency"] = conc
        status["inflight"] = inflight
        cap = 0
        need = 0
        page_sec = 0
        for beat in beats:
            cap += _as_int(beat.get("capacity_per_day"))
            need = max(need, _as_int(beat.get("browsers_need")))
            try:
                page_sec = float(beat.get("page_sec") or page_sec or 0)
            except (TypeError, ValueError):
                pass
        status["capacity_per_day"] = cap
        status["browsers_need"] = need
        status["page_sec"] = page_sec
        paused, pause_remaining = _pause_from_beats(beats)
        status["paused"] = paused
        status["pause_remaining_sec"] = pause_remaining
    else:
        beat = _loads(r.get(HB_DISCOVER_KEY)) or {}
        if beat and not running and not read_only:
            clear_discover_heartbeat(r)
            beat = {}
        status["heartbeat"] = beat
        status["current_url"] = _as_str(beat.get("url") or beat.get("current_url"))
        status["dir_fetched"] = _as_int(beat.get("dir_fetched"))
        status["persons_found"] = _as_int(beat.get("persons_found"))
        status["enqueued"] = _as_int(beat.get("enqueued"))
        status["letters"] = _as_str(beat.get("letters") or (saved.get("args") or {}).get("letters"))
    return status


def _start_process(cmd: List[str], log_name: str, register=None, env=None) -> int:
    if Path(log_name).name != log_name or log_name in {".", ".."}:
        raise ValueError("日志名称必须是当前日志目录内的文件名")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_DIR.is_symlink():
        raise ValueError("日志目录不能是符号链接")
    log_path = LOG_DIR / log_name
    if log_path.is_symlink():
        raise ValueError("日志文件不能是符号链接")
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("当前平台不支持安全打开日志文件")

    def open_owner_log():
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        directory_fd = os.open(LOG_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(log_name, flags, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("日志文件必须是普通且未硬链接的文件")
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "ab", buffering=0)
            fd = None
            return handle
        finally:
            if fd is not None:
                os.close(fd)

    # Restrict old logs before optional compaction, which preserves the mode.
    with open_owner_log():
        pass
    compact_inactive_log(log_path)
    with open_owner_log() as handle:
        handle.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode("utf-8"))
        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPTS),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        if register is not None:
            guard = None
            try:
                guard = psutil.Process(proc.pid)
                register(int(proc.pid))
            except Exception:
                # A launched child without its identity record cannot be safely
                # managed after a dashboard reload. Use the captured process
                # handle, which guards against PID reuse, to undo this launch.
                if guard is not None:
                    try:
                        _stop_owned_tree(
                            guard,
                            manager=str(SCRIPTS / "multi_worker_runner.py") in cmd,
                        )
                    except (OSError, psutil.Error):
                        try:
                            guard.terminate()
                        except psutil.Error:
                            pass
                else:
                    # Process lookup can fail after Popen but before Redis is
                    # written. The just-created Popen handle is the only safe
                    # fallback; never scan for a matching script name.
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                            proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        if proc.poll() is None:
                            proc.kill()
                    except OSError:
                        pass
                raise
    return int(proc.pid)


def _process_identity(pid: int, script_name: str):
    """Capture a stable birth time and this launcher's exact script/session."""
    if psutil is None:
        raise RuntimeError("缺少 psutil，进程身份无法核验；请先安装 psutil")
    try:
        process = psutil.Process(pid)
        args = process.cmdline()
        script_index = 2 if len(args) > 1 and args[1] == "-u" else 1
        if len(args) <= script_index or args[script_index] != str(SCRIPTS / script_name):
            return None
        if hasattr(os, "getsid") and hasattr(os, "getpgid"):
            if os.getsid(pid) != pid or os.getpgid(pid) != pid:
                return None
        return process, {
            "pid": pid,
            "create_time": process.create_time(),
            "script": str(SCRIPTS / script_name),
            "session_id": pid,
            "group_id": pid,
        }
    except (OSError, psutil.Error, AttributeError):
        return None


def _owned_process(saved: dict, script_name: str):
    identity = saved.get("identity") or {}
    pid = _as_int(saved.get("pid"))
    if not isinstance(identity, dict) or identity.get("pid") != pid or pid <= 0:
        return None
    if identity.get("script") != str(SCRIPTS / script_name):
        return None
    if identity.get("session_id") != pid or identity.get("group_id") != pid:
        return None
    current = _process_identity(pid, script_name)
    if not current or current[1]["create_time"] != identity.get("create_time"):
        return None
    return current[0]


def _record_started_process(r, key: str, pid: int, script_name: str, args: dict) -> None:
    current = _process_identity(pid, script_name)
    if not current:
        raise RuntimeError("子进程启动后无法核验脚本和会话身份；已拒绝保存不安全的 PID 记录")
    lock = _ACTIVE_START_LOCK.get()
    if not lock or lock[0] != f"{key}:start-lock" or lock[2].is_set():
        raise RuntimeError("启动锁已失效；已拒绝登记该子进程")
    payload = {
        "pid": pid,
        "identity": current[1],
        "started_at": int(time.time()),
        "args": args,
    }
    if r.eval(_REGISTER_START, 2, lock[0], key, lock[1], _dumps(payload)) != 1:
        raise RuntimeError("启动锁已失效；已拒绝登记该子进程")


def _live_same_process(process, create_time: float) -> bool:
    try:
        return process.is_running() and process.create_time() == create_time and process.status() != psutil.STATUS_ZOMBIE
    except (OSError, psutil.Error):
        return False


def _signal_same_process(process, create_time: float, sig: int) -> None:
    if _live_same_process(process, create_time):
        try:
            process.send_signal(sig)
        except (OSError, psutil.Error):
            pass


def _stop_owned_tree(process, manager: bool = False) -> List[int]:
    """Signal only verified process handles captured from the owned parent."""
    try:
        members = [process] + process.children(recursive=True)
        captured = [(child, child.create_time()) for child in members]
    except (OSError, psutil.Error):
        try:
            captured = [(process, process.create_time())]
        except (OSError, psutil.Error):
            return [process.pid]
    # Every root owns shutdown of its descendants. Signalling both parent and
    # children here can trip an emergency-exit handler on the second TERM.
    _signal_same_process(process, captured[0][1], signal.SIGTERM)
    deadline = time.monotonic() + (18 if manager else STOP_WAIT_SEC)
    while time.monotonic() < deadline:
        if not any(_live_same_process(child, created) for child, created in captured):
            break
        time.sleep(0.2)
    for child, created in reversed(captured):
        _signal_same_process(child, created, _SIGKILL)
    reap_deadline = time.monotonic() + 1
    for child, created in captured:
        remaining = reap_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            if child.create_time() == created:
                child.wait(timeout=remaining)
        except (OSError, psutil.Error):
            pass
    return [child.pid for child, created in captured if _live_same_process(child, created)]


@_serialized_start(CONTROL_WORKER_KEY)
def start_worker(r, concurrency: int = 2) -> dict:
    if psutil is None:
        raise RuntimeError("缺少 psutil，不能安全管理子进程；请先安装 psutil")
    conc = max(1, min(_as_int(concurrency, 2), MAX_BROWSER_CONCURRENCY))
    try:
        from tps_coverage import migrate_seen_to_queued
        migrate_seen_to_queued(r)
    except Exception:
        pass
    live = find_role_pids("worker")
    saved = _read_control(r, CONTROL_WORKER_KEY)
    owned = _owned_process(saved, "distributed_worker.py") if saved else None
    if live or owned:
        return {
            "ok": True,
            "already": True,
            "role": "worker",
            "pids": sorted(set([p["pid"] for p in live] + ([owned.pid] if owned else []))),
        }
    pid = _start_process(
        [
            sys.executable, "-u", str(SCRIPTS / "distributed_worker.py"),
            "--mode", "worker",
            "--concurrency", str(conc),
        ],
        "worker.log",
        lambda launched_pid: _record_started_process(
            r, CONTROL_WORKER_KEY, launched_pid, "distributed_worker.py", {"concurrency": conc}
        ),
    )
    return {"ok": True, "already": False, "role": "worker", "pid": pid, "concurrency": conc}


@_serialized_start(CONTROL_DISCOVER_KEY)
def start_discover(
    r,
    letters: str = "a",
    max_dir: int = 0,
    max_persons: int = 0,
    delay: float = 4.0,
    states: str = "",
    cities: str = "",
    age_min=None,
    age_max=None,
    reset_queue: bool = True,
) -> dict:
    if psutil is None:
        raise RuntimeError("缺少 psutil，不能安全管理子进程；请先安装 psutil")
    spec = normalize_letters(letters)
    md = max(0, _as_int(max_dir, 0))
    mp = max(0, _as_int(max_persons, 0))
    try:
        pause = float(delay)
    except (TypeError, ValueError):
        pause = 4.0
    pause = min(max(pause, 1.0), 30.0)

    live = find_role_pids("discover")
    saved = _read_control(r, CONTROL_DISCOVER_KEY)
    owned = _owned_process(saved, "discover.py") if saved else None
    if live or owned:
        return {
            "ok": True,
            "already": True,
            "role": "discover",
            "pids": sorted(set([p["pid"] for p in live] + ([owned.pid] if owned else []))),
        }

    from tps_coverage import migrate_seen_to_queued, normalize_slice, persist_slice, rebuild_discover_queue

    migrate_seen_to_queued(r)
    cfg = normalize_slice(spec, states, cities, age_min, age_max)
    persist_slice(r, cfg)
    rebuilt = rebuild_discover_queue(r, spec) if reset_queue else 0

    cmd = [
        sys.executable, "-u", str(SCRIPTS / "discover.py"),
        "--letters", spec,
        "--delay", str(pause),
        "--max-dir", str(md),
        "--max-persons", str(mp),
    ]
    if cfg["states"]:
        cmd += ["--states", ",".join(cfg["states"])]
    if cfg["cities"]:
        cmd += ["--cities", ",".join(cfg["cities"])]
    if cfg["age_min"] is not None:
        cmd += ["--age-min", str(cfg["age_min"])]
    if cfg["age_max"] is not None:
        cmd += ["--age-max", str(cfg["age_max"])]
    args = {
        "letters": spec,
        "max_dir": md,
        "max_persons": mp,
        "delay": pause,
        "states": cfg["states"],
        "cities": cfg["cities"],
        "age_min": cfg["age_min"],
        "age_max": cfg["age_max"],
        "rebuilt": rebuilt,
    }
    pid = _start_process(
        cmd, "discover.log",
        lambda launched_pid: _record_started_process(r, CONTROL_DISCOVER_KEY, launched_pid, "discover.py", args),
    )
    write_discover_heartbeat(r, {
        "pid": pid,
        "letters": spec,
        "url": "",
        "dir_fetched": 0,
        "persons_found": 0,
        "enqueued": 0,
        "skipped": 0,
        "status": "starting",
    })
    return {"ok": True, "already": False, "role": "discover", "pid": pid, "slice": cfg, **args}


def _stop_control(r, key: str, role: str, script_name: str, live_processes, manager: bool = False) -> dict:
    if psutil is None:
        return {"ok": False, "role": role, "stopped": [], "still_running": [], "error": "缺少 psutil，已拒绝按 PID 停止进程"}
    saved = _read_control(r, key)
    pid = _as_int(saved.get("pid"))
    process = _owned_process(saved, script_name) if saved else None
    if saved and pid_alive(pid) and not process:
        return {
            "ok": False, "role": role, "stopped": [], "still_running": [pid],
            "error": "进程身份与启动记录不一致，已拒绝向该 PID 发送信号",
        }
    leftover = _stop_owned_tree(process, manager=manager) if process else []
    if saved and not leftover:
        r.delete(key)
    other = [row["pid"] for row in live_processes() if row["pid"] != pid]
    still_running = sorted(set(leftover + other))
    if still_running:
        error = "仍有无法确认归属或尚未退出的进程，已拒绝向其发送信号"
    else:
        error = None
    return {
        "ok": not still_running,
        "role": role,
        "stopped": [pid] if process and pid not in leftover else [],
        "still_running": still_running,
        "error": error,
    }


@_serialized_start(CONTROL_WORKER_KEY, need_psutil=False)
def stop_worker(r) -> dict:
    result = _stop_control(r, CONTROL_WORKER_KEY, "worker", "distributed_worker.py", lambda: find_role_pids("worker"))
    if result["ok"]:
        for beat in _scan_heartbeats(r):
            clear_worker_heartbeat(r, _as_str(beat.get("worker_id")))
    return result


@_serialized_start(CONTROL_DISCOVER_KEY, need_psutil=False)
def stop_discover(r) -> dict:
    result = _stop_control(r, CONTROL_DISCOVER_KEY, "discover", "discover.py", lambda: find_role_pids("discover"))
    if result["ok"]:
        clear_discover_heartbeat(r)
    return result


# ============================================================
# 3000万/天 高通量多进程集群控制
# ============================================================

CONTROL_CLUSTER_KEY = "tps:control:cluster"
_CLUSTER_NEEDLE = "multi_worker_runner.py"
_PROTO_WORKER_NEEDLE = "protocol_worker.py"
_INGESTER_NEEDLE = "bulk_ingester_daemon.py"


def find_cluster_pids() -> List[dict]:
    found = []
    for row in _list_ps():
        cmd = row["cmd"]
        if (
            _project_script_in_command(cmd, _CLUSTER_NEEDLE)
            or _project_script_in_command(cmd, _PROTO_WORKER_NEEDLE)
            or _project_script_in_command(cmd, _INGESTER_NEEDLE)
        ) and pid_alive(row["pid"]):
            found.append(row)
    return found


@_serialized_start(CONTROL_CLUSTER_KEY)
def start_cluster(
    r,
    workers: int = 4,
    concurrency: int = 80,
    decoupled: bool = True,
    proxy_tunnel: Optional[str] = None,
) -> dict:
    if psutil is None:
        raise RuntimeError("缺少 psutil，不能安全管理子进程；请先安装 psutil")
    if not decoupled:
        return {
            "ok": False, "already": False, "role": "cluster",
            "error": "直接内存批量写库模式已禁用；请使用可靠缓冲入库守护进程",
        }
    w = max(1, min(_as_int(workers, 4), 32))
    c = max(1, min(_as_int(concurrency, 2), 500))

    live = [p for p in find_cluster_pids() if _CLUSTER_NEEDLE in p["cmd"]]
    saved = _read_control(r, CONTROL_CLUSTER_KEY)
    owned = _owned_process(saved, "multi_worker_runner.py") if saved else None
    if live or owned:
        return {
            "ok": True,
            "already": True,
            "role": "cluster",
            "pids": sorted(set([p["pid"] for p in live] + ([owned.pid] if owned else []))),
        }

    # Protocol workers consume tps:pending and ack captcha pages as empty.
    if find_role_pids("worker"):
        return {
            "ok": False,
            "already": False,
            "role": "cluster",
            "error": "浏览器 Worker 已在运行，不再启动协议集群",
        }

    cmd = [
        sys.executable,
        "-u",
        str(SCRIPTS / "multi_worker_runner.py"),
        "--workers",
        str(w),
        "--concurrency",
        str(c),
    ]
    launch_env = os.environ.copy()
    launch_env["TPS_ALLOW_CLUSTER"] = "1"
    if proxy_tunnel and proxy_tunnel.strip():
        launch_env["PROXY_TUNNEL"] = proxy_tunnel.strip()

    pid = _start_process(
        cmd, "cluster.log",
        lambda launched_pid: _record_started_process(
            r, CONTROL_CLUSTER_KEY, launched_pid, "multi_worker_runner.py", {
                "workers": w,
                "concurrency": c,
                "decoupled": decoupled,
                "proxy_mode": "tunnel" if launch_env.get("PROXY_TUNNEL") else "dynamic",
            },
        ),
        env=launch_env,
    )
    return {
        "ok": True,
        "already": False,
        "role": "cluster",
        "pid": pid,
        "workers": w,
        "concurrency": c,
    }


@_serialized_start(CONTROL_CLUSTER_KEY, need_psutil=False)
def stop_cluster(r) -> dict:
    result = _stop_control(r, CONTROL_CLUSTER_KEY, "cluster", "multi_worker_runner.py", find_cluster_pids, manager=True)
    if result["ok"]:
        for beat in _scan_heartbeats(r):
            clear_worker_heartbeat(r, _as_str(beat.get("worker_id")))
    return result


def _safe_cluster_log_line(line: str) -> str:
    if "代理模式配置:" in line:
        return line.split("代理模式配置:", 1)[0] + "代理模式配置: 已配置"
    # Historical logs could contain proxy API URLs with tokens in query strings.
    # Do not try to enumerate credential parameter names; hide the entire URL.
    return re.sub(r"(?i)\b(?:https?|socks5)://\S+", "[URL 已隐藏]", line)


def bulk_ingest_status(r) -> dict:
    """只读取一次独立入库守护进程的共享计数和短期速率。"""
    total_raw = r.get(BULK_COMMITTED_TOTAL_KEY)
    total = _as_int(total_raw) if total_raw is not None else None
    snapshot = _loads(r.get(BULK_RATE_KEY))
    result = {
        "rate_available": False, "qps": None, "committed_total": total,
        "db_ready": False, "db_target": None, "pid": None,
    }
    if not snapshot:
        return result
    try:
        qps = float(snapshot["qps"])
        updated_at = float(snapshot["updated_at"])
        age = time.time() - updated_at
    except (KeyError, TypeError, ValueError, OverflowError):
        return result
    if not math.isfinite(qps) or qps < 0 or not math.isfinite(age) or age < -2 or age > BULK_RATE_TTL_SEC:
        return result
    result["db_ready"] = snapshot.get("db_ready") is True
    result["db_target"] = snapshot.get("db_target") if isinstance(snapshot.get("db_target"), str) else None
    result["pid"] = _as_int(snapshot.get("pid")) or None
    if not result["db_ready"]:
        return result
    result["rate_available"] = True
    result["qps"] = qps
    if total is None:
        result["committed_total"] = _as_int(snapshot.get("committed_total"))
    return result


def cluster_status(r) -> dict:
    saved = _read_control(r, CONTROL_CLUSTER_KEY)
    public_args = dict(saved.get("args") or {})
    for secret_field in ("proxy_tunnel", "proxy_api"):
        if secret_field in public_args:
            public_args[secret_field] = "已配置" if public_args[secret_field] else ""
    all_cluster = find_cluster_pids()
    runners = [p for p in all_cluster if _CLUSTER_NEEDLE in p["cmd"]]
    proto_workers = [p for p in all_cluster if _PROTO_WORKER_NEEDLE in p["cmd"]]
    ingesters = [p for p in all_cluster if _INGESTER_NEEDLE in p["cmd"]]

    beats = _scan_heartbeats(r)
    try:
        bulk = bulk_ingest_status(r)
    except Exception:
        bulk = {"rate_available": False, "qps": None, "committed_total": None}
    running = bool(runners or proto_workers or ingesters or beats or bulk["rate_available"])

    direct_qps = 0.0
    worker_rates_valid = True
    total_concurrency = 0
    inflight_count = 0
    for beat in beats:
        try:
            rate = float(beat["current_qps"])
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("invalid worker rate")
            direct_qps += rate
        except (KeyError, TypeError, ValueError, OverflowError):
            worker_rates_valid = False
        total_concurrency += int(beat.get("concurrency") or 0)
        inflight_count += len(beat.get("inflight") or [])

    expects_bulk = bool(ingesters) or any(beat.get("decoupled_ingest") is True for beat in beats)
    throughput_available = (
        not running
        or (worker_rates_valid and (beats or bulk["rate_available"]) and (not expects_bulk or bulk["rate_available"]))
    )
    total_qps = (
        round(direct_qps + (bulk["qps"] or 0.0), 2)
        if throughput_available and running else (0.0 if not running else None)
    )

    buffer_depth = None
    try:
        buffer_depth = _as_int(r.llen("tps:buffer:parsed")) + _as_int(r.llen("tps:buffer:processing"))
    except Exception:
        pass

    return {
        "running": running,
        "runner_pids": [p["pid"] for p in runners],
        "worker_pids": [p["pid"] for p in proto_workers],
        "ingester_pids": [p["pid"] for p in ingesters],
        "workers_count": len(proto_workers),
        "total_concurrency": total_concurrency,
        "total_qps": total_qps,
        "throughput_available": throughput_available,
        "total_qps_meaning": "database_persisted_confirmed_per_second",
        "bulk_ingest_qps": bulk["qps"],
        "bulk_committed_total": bulk["committed_total"],
        "target_qps": 347.2,
        "target_progress_pct": round(min(100.0, (total_qps / 347.2) * 100), 1) if total_qps is not None else None,
        "inflight_count": inflight_count,
        "buffer_depth": buffer_depth,
        "buffer_available": buffer_depth is not None,
        "started_at": saved.get("started_at"),
        "args": public_args,
        "heartbeats": beats,
        "logs": [_safe_cluster_log_line(line) for line in _tail_log(LOG_DIR / "cluster.log", 40)],
    }


def pipeline_status(r, *, read_only: bool = False) -> dict:
    """Report pipeline state without mutating legacy queue records when requested."""
    queue = queue_stats(r)
    try:
        discover_pending = _as_int(r.llen(DISCOVER_PENDING))
        discover_seen = _as_int(r.scard(DISCOVER_SEEN))
    except Exception:
        discover_pending = 0
        discover_seen = 0

    metrics = {}
    try:
        from tps_metrics import get_metrics
        metrics = get_metrics(r).snapshot()
    except Exception:
        metrics = {"counters": {}, "latency": {}, "ts": int(time.time())}

    coverage = {}
    try:
        from tps_coverage import coverage_snapshot, migrate_seen_to_queued
        if not read_only:
            migrate_seen_to_queued(r)
        coverage = coverage_snapshot(r)
    except Exception as exc:
        coverage = {"error": str(exc)}

    discover_dirs = {}
    try:
        from tps_coverage import discover_summary
        discover_dirs = discover_summary(r)
    except Exception:
        discover_dirs = {}

    return {
        "redis_ok": True,
        "ts": int(time.time()),
        "queue": queue,
        "discover_pending": discover_pending,
        "discover_seen": discover_seen,
        "discover_dirs": discover_dirs,
        "worker": _role_status(r, "worker", read_only=read_only),
        "discover": _role_status(r, "discover", read_only=read_only),
        "cluster": cluster_status(r),
        "jobs": peek_processing(r, 20),
        "dlq_jobs": peek_dlq(r, 8),
        "metrics": metrics,
        "coverage": coverage,
        "logs": {
            "worker": [_safe_cluster_log_line(line) for line in _tail_log(LOG_DIR / "worker.log")],
            "discover": [_safe_cluster_log_line(line) for line in _tail_log(LOG_DIR / "discover.log")],
            "cluster": [_safe_cluster_log_line(line) for line in _tail_log(LOG_DIR / "cluster.log")],
        },
    }
