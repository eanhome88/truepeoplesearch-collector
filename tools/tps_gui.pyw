# -*- coding: utf-8 -*-
"""
TPS 极速采集控制中心 (Windows 桌面可视化控制台)
免 cmd 命令行黑框，解决编码与换行符问题，一键控制启动、暂停与实时数据大屏。
"""
import os
import sys
import time
import socket
import threading
import subprocess
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox
import pymysql

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(ROOT_DIR, ".venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable

REDIS_EXE = r"D:\tps\redis\redis-server.exe"
SCRAPE_SCRIPT = os.path.join(ROOT_DIR, "scripts", "scrape_to_tidb.py")
DISCOVER_SCRIPT = os.path.join(ROOT_DIR, "scripts", "phone_discover.py")
SUPERVISOR_SCRIPT = os.path.join(ROOT_DIR, "scripts", "tps_supervisor.py")
DASHBOARD_SCRIPT = os.path.join(ROOT_DIR, "tools", "dashboard_api.py")

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "tps123456",
    "database": "people_search",
    "connect_timeout": 3
}

def get_db_conn():
    try:
        import mysql.connector
        return mysql.connector.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            database=DB_CONFIG["database"],
            connection_timeout=DB_CONFIG["connect_timeout"]
        )
    except Exception:
        import pymysql
        return pymysql.connect(**DB_CONFIG)

class TPSApp:
    def __init__(self, root):
        self.root = root
        self.root.title("TPS 极速采集控制中心")
        self.root.geometry("480x460")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")

        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.create_widgets()
        self.refresh_stats()
        self.start_auto_refresh()

    def create_widgets(self):
        title_frame = tk.Frame(self.root, bg="#1e1e2e")
        title_frame.pack(fill="x", pady=15)
        tk.Label(title_frame, text="⚡ TruePeopleSearch 极速采集控制中心", font=("Microsoft YaHei UI", 15, "bold"), fg="#cdd6f4", bg="#1e1e2e").pack()
        tk.Label(title_frame, text="AMD EPYC 专用 32路/64路并发引擎", font=("Microsoft YaHei UI", 9), fg="#a6adc8", bg="#1e1e2e").pack()

        stat_frame = tk.LabelFrame(self.root, text=" 📊 数据库实时入库数据 ", font=("Microsoft YaHei UI", 10, "bold"), fg="#89b4fa", bg="#181825", bd=1, relief="solid")
        stat_frame.pack(fill="x", padx=20, pady=5)

        self.lbl_total = tk.Label(stat_frame, text="• 总人物档案库:       -- 条", font=("Microsoft YaHei UI", 11), fg="#cdd6f4", bg="#181825", anchor="w")
        self.lbl_total.pack(fill="x", padx=15, pady=4)

        self.lbl_phones = tk.Label(stat_frame, text="• 拥有有效电话人物:   -- 条", font=("Microsoft YaHei UI", 11), fg="#a6e3a1", bg="#181825", anchor="w")
        self.lbl_phones.pack(fill="x", padx=15, pady=4)

        self.lbl_wireless = tk.Label(stat_frame, text="• 真实手机(Wireless): -- 条", font=("Microsoft YaHei UI", 11, "bold"), fg="#f9e2af", bg="#181825", anchor="w")
        self.lbl_wireless.pack(fill="x", padx=15, pady=4)

        ctrl_frame = tk.Frame(self.root, bg="#1e1e2e")
        ctrl_frame.pack(fill="x", padx=20, pady=15)

        btn_start = tk.Button(ctrl_frame, text="▶  一键启动全部 (Redis + 32路采集)", font=("Microsoft YaHei UI", 11, "bold"), bg="#a6e3a1", fg="#11111b", activebackground="#94e2d5", height=2, cursor="hand2", command=self.start_all)
        btn_start.pack(fill="x", pady=4)

        btn_stop = tk.Button(ctrl_frame, text="⏸  一键暂停 / 停止采集", font=("Microsoft YaHei UI", 10), bg="#f38ba8", fg="#11111b", activebackground="#eba0ac", height=1, cursor="hand2", command=self.stop_all)
        btn_stop.pack(fill="x", pady=4)

        btn_dash = tk.Button(ctrl_frame, text="🌐  打开 Web 监控大屏 (端口 5001)", font=("Microsoft YaHei UI", 10), bg="#89b4fa", fg="#11111b", activebackground="#74c7ec", height=1, cursor="hand2", command=self.open_dashboard)
        btn_dash.pack(fill="x", pady=4)

        bottom_frame = tk.Frame(self.root, bg="#1e1e2e")
        bottom_frame.pack(fill="x", padx=20, pady=5)

        tk.Checkbutton(bottom_frame, text="每 5 秒自动更新数据", variable=self.auto_refresh_var, font=("Microsoft YaHei UI", 9), fg="#cdd6f4", bg="#1e1e2e", selectcolor="#181825", activebackground="#1e1e2e", activeforeground="#cdd6f4").pack(side="left")

        tk.Button(bottom_frame, text="🔄 立即刷新", font=("Microsoft YaHei UI", 9), bg="#45475a", fg="#cdd6f4", command=self.refresh_stats).pack(side="right")

        self.lbl_status = tk.Label(self.root, text="就绪", font=("Microsoft YaHei UI", 9), fg="#6c7086", bg="#1e1e2e", anchor="w")
        self.lbl_status.pack(fill="x", padx=20, pady=5)

    def set_status(self, text, color="#a6adc8"):
        self.lbl_status.config(text=f"状态: {text}", fg=color)

    def start_all(self):
        threading.Thread(target=self._start_all_thread, daemon=True).start()

    def _start_all_thread(self):
        self.set_status("正在启动 TPS 进程管理器 (32路并发)...", "#89b4fa")
        try:
            bat_path = os.path.join(ROOT_DIR, "start_client.bat")
            if os.path.exists(bat_path):
                subprocess.Popen(["cmd.exe", "/c", bat_path], cwd=ROOT_DIR)
            else:
                subprocess.Popen(["cmd.exe", "/c", "start", "TPS-Supervisor", VENV_PYTHON, SUPERVISOR_SCRIPT, "--workers", "32"], cwd=ROOT_DIR)
            self.set_status("全部任务已成功启动！正在极速抓取中...", "#a6e3a1")
        except Exception as e:
            self.set_status(f"启动失败: {e}", "#f38ba8")

    def stop_all(self):
        try:
            bat_path = os.path.join(ROOT_DIR, "stop_client.bat")
            if os.path.exists(bat_path):
                subprocess.run(["cmd.exe", "/c", bat_path], cwd=ROOT_DIR, capture_output=True)
            else:
                subprocess.run("taskkill /f /fi \"WINDOWTITLE eq TPS-*\"", shell=True, capture_output=True)
            self.set_status("已安全停止所有采集与发现进程", "#f9e2af")
        except Exception as e:
            self.set_status(f"停止失败: {e}", "#f38ba8")

    def open_dashboard(self):
        threading.Thread(target=self._open_dashboard_thread, daemon=True).start()

    def _open_dashboard_thread(self):
        self.set_status("正在检查 Web 面板服务 (5001)...", "#89b4fa")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1.0)
            res = s.connect_ex(("127.0.0.1", 5001))
            s.close()
            if res != 0:
                subprocess.Popen(["cmd.exe", "/c", "start", "/min", "TPS-Dashboard", VENV_PYTHON, DASHBOARD_SCRIPT, "--port", "5001"], cwd=ROOT_DIR)
                time.sleep(2)
            webbrowser.open("http://127.0.0.1:5001")
            self.set_status("Web 面板已在浏览器打开 (http://127.0.0.1:5001)", "#a6e3a1")
        except Exception as e:
            self.set_status(f"打开面板失败: {e}", "#f38ba8")

    def refresh_stats(self):
        threading.Thread(target=self._refresh_stats_thread, daemon=True).start()

    def _refresh_stats_thread(self):
        try:
            conn = get_db_conn()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM persons")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(DISTINCT person_id) FROM phone_numbers")
            phones = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM phone_numbers WHERE line_type='Wireless'")
            wireless = cur.fetchone()[0]
            conn.close()
            self.root.after(0, lambda: self._update_stats_ui(total, phones, wireless))
        except Exception as e:
            self.root.after(0, lambda: self.set_status(f"数据库连接提示: {e}", "#f38ba8"))

    def _update_stats_ui(self, total, phones, wireless):
        self.lbl_total.config(text=f"• 总人物档案库:       {total:,} 条")
        self.lbl_phones.config(text=f"• 拥有有效电话人物:   {phones:,} 条")
        self.lbl_wireless.config(text=f"• 真实手机(Wireless): {wireless:,} 条")

    def start_auto_refresh(self):
        if self.auto_refresh_var.get():
            self.refresh_stats()
        self.root.after(5000, self.start_auto_refresh)

if __name__ == "__main__":
    root = tk.Tk()
    app = TPSApp(root)
    root.mainloop()
