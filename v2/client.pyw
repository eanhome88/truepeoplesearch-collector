# -*- coding: utf-8 -*-
"""高性能采集客户端。采集按本机自动扩容；查看和导出在后台分页，不堵住窗口。"""

import csv
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
PYDEPS = ROOT / "pydeps"
if PYDEPS.is_dir():
    sys.path.insert(0, str(PYDEPS))
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ROOT))

from tps_env import load_project_env
from query import COLUMNS, PAGE_SIZE, build_filter, page_sql

load_project_env(ROOT, customer_safe=False)
if not os.environ.get("CLOUDBYPASS_APIKEY"):
    load_project_env(ROOT.parent, customer_safe=False)

PAUSE_FILE = ROOT / "data" / "client.pause"
PID_FILE = ROOT / "data" / "client_pids.json"


def python_exe():
    marker = ROOT / "python.path"
    if marker.is_file():
        chosen = marker.read_text(encoding="utf-8").strip().strip('"')
        if chosen and Path(chosen).is_file():
            return chosen
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT.parent / ".venv" / "Scripts" / "python.exe",
        ROOT.parent / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def connect_db():
    import mysql.connector

    return mysql.connector.connect(
        host=os.environ.get("TPS_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TPS_DB_PORT", "3306")),
        user=os.environ.get("TPS_DB_USER", "root"),
        password=os.environ.get("TPS_DB_PASSWORD", ""),
        database=os.environ.get("TPS_DB_NAME", "people_search"),
        connection_timeout=5,
        autocommit=True,
    )


def connect_redis():
    import redis

    return redis.Redis(
        host=os.environ.get("TPS_REDIS_HOST") or os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("TPS_REDIS_PORT") or os.environ.get("REDIS_PORT", "6379")),
        password=os.environ.get("TPS_REDIS_PASSWORD") or os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


class Client(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("采集客户端")
        self.geometry("980x640")
        self.minsize(860, 560)
        self.procs = {}
        self.busy = False
        self.page_after = None
        self.page_stack = []
        self.page_rows = []
        self.has_more = False
        self._load_pids()
        self._build()
        self.after(300, self.refresh_progress)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="开始", command=self.start).pack(side="left", padx=4)
        ttk.Button(bar, text="暂停", command=self.pause).pack(side="left", padx=4)
        ttk.Button(bar, text="继续", command=self.resume).pack(side="left", padx=4)
        ttk.Button(bar, text="停止", command=self.stop).pack(side="left", padx=4)
        ttk.Button(bar, text="诊断修复", command=self.diagnose).pack(side="left", padx=4)
        self.status = ttk.Label(bar, text="未启动")
        self.status.pack(side="left", padx=12)

        self.progress = ttk.Label(self, padding=(12, 0), text="进度：正在读取…")
        self.progress.pack(fill="x")

        filt = ttk.LabelFrame(self, text="筛选（姓名和电话按开头匹配）", padding=8)
        filt.pack(fill="x", padx=8, pady=8)
        self.name = self._field(filt, "姓名")
        self.state = self._field(filt, "州")
        self.phone = self._field(filt, "电话")
        self.wireless = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="只看无线手机", variable=self.wireless).pack(side="left", padx=8)
        self.btn_search = ttk.Button(filt, text="查看", command=self.search_first)
        self.btn_search.pack(side="left", padx=4)
        self.btn_export = ttk.Button(filt, text="导出", command=self.export)
        self.btn_export.pack(side="left", padx=4)

        table_frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        table_frame.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_frame, columns=COLUMNS, show="headings")
        for name, width in zip(COLUMNS, (160, 50, 50, 140, 90, 360)):
            self.table.heading(name, text=name)
            self.table.column(name, width=width, anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        foot = ttk.Frame(self, padding=(12, 0, 12, 8))
        foot.pack(fill="x")
        self.count = ttk.Label(foot, text="当前列表：0 条")
        self.count.pack(side="left")
        self.btn_prev = ttk.Button(foot, text="上一页", command=self.page_prev, state="disabled")
        self.btn_prev.pack(side="right", padx=4)
        self.btn_next = ttk.Button(foot, text="下一页", command=self.page_next, state="disabled")
        self.btn_next.pack(side="right", padx=4)

    def _field(self, parent, label):
        ttk.Label(parent, text=label).pack(side="left")
        var = tk.StringVar()
        ttk.Entry(parent, textvariable=var, width=16).pack(side="left", padx=(4, 10))
        return var

    def _filters(self):
        return build_filter(
            self.name.get(),
            self.state.get(),
            self.phone.get(),
            self.wireless.get(),
        )

    def _load_pids(self):
        try:
            saved = json.loads(PID_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        self.procs = {name: pid for name, pid in saved.items() if pid_alive(pid)}

    def _save_pids(self):
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(json.dumps(self.procs), encoding="utf-8")

    def _running(self):
        self.procs = {name: pid for name, pid in self.procs.items() if pid_alive(pid)}
        return bool(self.procs)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_search.configure(state=state)
        self.btn_export.configure(state=state)

    def start(self):
        if self._running():
            self.resume()
            return
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
        exe = python_exe()
        env = os.environ.copy()
        env.setdefault("USE_CLOUDBYPASS", "1")
        env.setdefault("TPS_RELEASE_MODE", "standard")
        if PYDEPS.is_dir():
            env["PYTHONPATH"] = str(PYDEPS) + os.pathsep + env.get("PYTHONPATH", "")
        flags = 0x08000000 if sys.platform == "win32" else 0
        (ROOT / "data").mkdir(parents=True, exist_ok=True)
        jobs = {
            "feeder": [exe, str(ENGINE / "phone_discover.py"), "--start-area", "201"],
            "worker": [exe, str(ENGINE / "fast_worker.py")],
        }
        try:
            self._log_handles = []
            for name, cmd in jobs.items():
                handle = open(ROOT / "data" / f"{name}.log", "a", encoding="utf-8", errors="replace")
                handle.write("\n--- start ---\n")
                handle.flush()
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT),
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
                self._log_handles.append(handle)
                self.procs[name] = proc.pid
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self._save_pids()
        self.status.configure(text="运行中 · 穿云高速，目标每天约 50 万条")

    def pause(self):
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.write_text("paused", encoding="utf-8")
        self.status.configure(text="已暂停 · 不领新任务")

    def resume(self):
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
        if not self._running():
            self.start()
            return
        self.status.configure(text="运行中 · 穿云高速，目标每天约 50 万条")

    def diagnose(self):
        if self.busy:
            return
        self._set_busy(True)
        self.status.configure(text="正在诊断…")

        def work():
            from doctor import run_doctor
            notes = run_doctor(ROOT)
            self.after(0, lambda: self._show_notes(notes))

        threading.Thread(target=self._guard, args=(work, "诊断失败"), daemon=True).start()

    def _show_notes(self, notes):
        self._set_busy(False)
        text = "\n".join(notes) or "没有发现需要处理的问题。"
        self.status.configure(text="诊断完成")
        messagebox.showinfo("诊断修复", text)

    def stop(self):
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
        for pid in list(self.procs.values()):
            self._kill(pid)
        self.procs = {}
        self._save_pids()
        self.status.configure(text="已停止")

    def _kill(self, pid):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass

    def refresh_progress(self):
        if not getattr(self, "_progress_running", False):
            self._progress_running = True
            threading.Thread(target=self._read_progress, daemon=True).start()
        self.after(2000, self.refresh_progress)

    def _read_progress(self):
        people = phones = queue = "—"
        cursor = "未开始"
        credits = "0"
        try:
            db = connect_db()
            cur = db.cursor()
            cur.execute(
                "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ('persons', 'phone_numbers')"
            )
            counts = {name: int(rows or 0) for name, rows in cur.fetchall()}
            people = f"{counts.get('persons', 0):,}"
            phones = f"{counts.get('phone_numbers', 0):,}"
            cur.close()
            db.close()
        except Exception:
            pass
        try:
            r = connect_redis()
            queue = f"{int(r.llen('tps:pending') or 0):,}"
            cursor = r.get("tps:phone:cursor") or "未开始"
            spent = r.hget("tps:credit", "points")
            credits = f"{int(spent or 0):,}"
            r.close()
        except Exception:
            pass
        self.after(0, lambda: self._show_progress(people, phones, queue, cursor, credits))

    def _show_progress(self, people, phones, queue, cursor, credits):
        self._progress_running = False
        if PAUSE_FILE.exists() and self._running():
            state = "已暂停 · 不领新任务"
        elif self._running():
            state = "运行中 · 穿云高速，目标每天约 50 万条"
        else:
            state = "未启动"
        if not self.busy:
            self.status.configure(text=state)
        self.progress.configure(
            text=(
                f"进度：人物约 {people}    电话约 {phones}    待查队列 {queue}    "
                f"号段 {cursor}    预计积分 {credits}"
            )
        )

    def search_first(self):
        self.page_stack = []
        self.page_after = None
        self._load_page(None, reset_stack=True)

    def page_next(self):
        if not self.has_more or not self.page_rows:
            return
        self.page_stack.append(self.page_after)
        last_id = self.page_rows[PAGE_SIZE - 1][0]
        self._load_page(last_id, reset_stack=False)

    def page_prev(self):
        if not self.page_stack:
            return
        previous = self.page_stack.pop()
        self._load_page(previous, reset_stack=False, keep_stack=True)

    def _load_page(self, after_id, reset_stack, keep_stack=False):
        if self.busy:
            return
        where, params = self._filters()
        sql, extra = page_sql(where, after_id)
        self._set_busy(True)
        self.count.configure(text="正在查询…")

        def work():
            db = connect_db()
            try:
                cur = db.cursor()
                cur.execute(sql, params + extra)
                rows = cur.fetchall()
                cur.close()
            finally:
                db.close()
            self.after(0, lambda: self._show_page(rows, after_id, reset_stack, keep_stack))

        threading.Thread(target=self._guard, args=(work, "查看失败"), daemon=True).start()

    def _show_page(self, rows, after_id, reset_stack, keep_stack):
        self._set_busy(False)
        has_more = len(rows) > PAGE_SIZE
        visible = rows[:PAGE_SIZE]
        self.has_more = has_more
        self.page_rows = visible
        self.page_after = after_id
        if reset_stack:
            self.page_stack = []
        self.table.delete(*self.table.get_children())
        for row in visible:
            shown = tuple("" if value is None else value for value in row[1:])
            self.table.insert("", "end", values=shown)
        self.count.configure(text=f"本页 {len(visible)} 条")
        self.btn_next.configure(state="normal" if has_more else "disabled")
        self.btn_prev.configure(state="normal" if self.page_stack or after_id else "disabled")

    def export(self):
        if self.busy:
            return
        path = filedialog.asksaveasfilename(
            title="导出筛选结果",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="leads.csv",
        )
        if not path:
            return
        where, params = self._filters()
        self._set_busy(True)

        def work():
            db = connect_db()
            count = 0
            try:
                cur = db.cursor()
                after_id = None
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(COLUMNS)
                    while True:
                        sql, extra = page_sql(where, after_id)
                        cur.execute(sql, params + extra)
                        rows = cur.fetchall()
                        if not rows:
                            break
                        batch = rows[:PAGE_SIZE]
                        writer.writerows(
                            tuple("" if value is None else value for value in row[1:])
                            for row in batch
                        )
                        count += len(batch)
                        after_id = batch[-1][0]
                        self.after(0, lambda n=count: self.count.configure(text=f"已导出 {n:,} 条…"))
                        if len(rows) <= PAGE_SIZE:
                            break
                cur.close()
            finally:
                db.close()
            self.after(0, lambda: self._export_done(count, path))

        threading.Thread(target=self._guard, args=(work, "导出失败"), daemon=True).start()

    def _export_done(self, count, path):
        self._set_busy(False)
        self.count.configure(text=f"已导出 {count:,} 条")
        messagebox.showinfo("导出完成", f"已导出 {count:,} 条\n{path}")

    def _guard(self, work, title):
        try:
            work()
        except Exception as exc:
            self.after(0, lambda: self._fail(title, exc))

    def _fail(self, title, exc):
        self._set_busy(False)
        messagebox.showerror(title, str(exc))

    def _close(self):
        self._save_pids()
        self.destroy()


if __name__ == "__main__":
    Client().mainloop()
