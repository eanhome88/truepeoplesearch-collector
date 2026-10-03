# -*- coding: utf-8 -*-
"""Windows 安装入口。不创建 venv，失败时弹出具体原因。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def say(title, text, error=False):
    log = ROOT / "data" / "install.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("\n" + text + "\n")
    try:
        import tkinter as tk
        from tkinter import messagebox
        hold = tk.Tk()
        hold.withdraw()
        if error:
            messagebox.showerror(title, text)
        else:
            messagebox.showinfo(title, text)
        hold.destroy()
    except Exception:
        print(text)


def find_python():
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.extend(Path(local, "Programs", "Python").glob("Python3*/python.exe"))
    candidates.extend(Path(r"C:\Program Files\Python").glob("Python3*/python.exe"))
    candidates.extend(Path(r"C:\Python").glob("Python3*/python.exe"))
    for path in candidates:
        if path.is_file() and "WindowsApps" not in str(path):
            return str(path)
    return ""


def main():
    current = str(Path(sys.executable).resolve())
    if "WindowsApps" in current:
        other = find_python()
        if not other:
            say(
                "安装失败",
                "现在用的是微软商店的 Python 占位程序，不能安装依赖。\n"
                "请到 https://www.python.org/downloads/windows/ 安装 Python 3.9 或更高版本，\n"
                "勾选 Add python.exe to PATH，然后再双击 install.bat。",
                error=True,
            )
            return 1
        os.execv(other, [other, str(Path(__file__).resolve())])

    pydeps = ROOT / "pydeps"
    pydeps.mkdir(parents=True, exist_ok=True)
    command = [
        current, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"), "-t", str(pydeps),
    ]
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "install.log").write_text(output, encoding="utf-8")
    if result.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-25:])
        say("安装失败", "依赖没有装完。最后几行是：\n\n" + tail + "\n\n完整记录在 data\\install.log", error=True)
        return 1

    (ROOT / "python.path").write_text(current, encoding="utf-8")
    env_file = ROOT / ".env"
    if not env_file.is_file():
        parent = ROOT.parent / ".env"
        source = parent if parent.is_file() else ROOT / "runtime.env.example"
        if source.is_file():
            shutil.copyfile(source, env_file)
    say("安装完成", "依赖已装好。\n请确认 MySQL 和 Redis 已启动，然后双击 start.bat。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
