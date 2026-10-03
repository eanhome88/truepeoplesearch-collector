"""识别已经见过的环境故障并直接处理。未知报错只在配置了诊断接口时请求说明。"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def tail_text(path: Path, limit: int = 80) -> str:
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def explain_log(log: str, environ=None) -> str:
    env = os.environ if environ is None else environ
    key = (env.get("DOCTOR_API_KEY") or "").strip()
    if not key or not log.strip():
        return ""
    base = (env.get("DOCTOR_API_BASE") or "https://api.openai.com/v1").rstrip("/")
    model = (env.get("DOCTOR_MODEL") or "gpt-4o-mini").strip()
    payload = json.dumps({
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是采集客户端的诊断助手。根据日志用简体中文说明原因和下一步，不超过120字。不要输出代码，不要给出会删除数据的命令。",
            },
            {"role": "user", "content": log[-4000:]},
        ],
    }).encode("utf-8")
    request = urllib.request.Request(
        base + "/chat/completions",
        data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"].strip()
    except (KeyError, TypeError, ValueError, urllib.error.URLError, TimeoutError):
        return ""


def run_doctor(root: Path) -> list:
    root = Path(root)
    notes = []
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)

    if not (root / "python.path").is_file() or not (root / "pydeps" / "httpx" / "__init__.py").is_file():
        notes.append("依赖还没装好。请先双击 install.bat。")
    else:
        notes.append("Python 和依赖目录存在。")

    env_file = root / ".env"
    if not env_file.is_file():
        example = root / "runtime.env.example"
        parent = root.parent / ".env"
        source = parent if parent.is_file() else example
        if source.is_file():
            env_file.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            notes.append("已补上 .env。")
        else:
            notes.append("没有 .env，无法填写密钥。")

    values = _env_map(env_file)
    for name in ("CLOUDBYPASS_APIKEY", "TPS_DB_HOST", "TPS_DB_NAME"):
        if not values.get(name):
            notes.append(f".env 里 {name} 是空的，需要手工填写。")
    if not values.get("CLOUDBYPASS_PROXY") and not values.get("PROXY_TUNNEL"):
        notes.append(".env 里没有动态代理地址。")

    pid_file = data / "client_pids.json"
    if pid_file.is_file():
        try:
            saved = json.loads(pid_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            saved = None
        if not isinstance(saved, dict):
            pid_file.write_text("{}", encoding="utf-8")
            notes.append("已清掉损坏的进程记录。")

    log = "\n".join(
        part for part in (
            tail_text(data / "worker.log"),
            tail_text(data / "feeder.log"),
        ) if part
    )
    if "INSUFFICIENT_BALANCE" in log:
        notes.append("穿云积分不足。程序无法自行充值。")
    if "Redis" in log and "不可用" in log:
        notes.append("Redis 没连上。请先启动本机 Redis。")
    if "Traceback" in log and "No module named" in log and (root / "python.path").is_file():
        if _reinstall(root):
            notes.append("已按报错重装依赖。请再点开始。")
        else:
            notes.append("缺 Python 包，自动重装没有成功。请再运行 install.bat。")
    advice = explain_log(log)
    if advice:
        notes.append("诊断说明：" + advice)
    elif "Traceback" in log and not (os.environ.get("DOCTOR_API_KEY") or "").strip():
        notes.append("日志里有程序报错。配置 DOCTOR_API_KEY 后，诊断会说明原因；不会自动改代码。")
    if len(notes) == 1 and notes[0].startswith("Python"):
        notes.append("没有发现需要自动处理的故障。")
    return notes


def _env_map(path: Path) -> dict:
    found = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        found[name.strip()] = value.strip().strip("'\"")
    return found


def _reinstall(root: Path) -> bool:
    import subprocess
    python = (root / "python.path").read_text(encoding="utf-8").strip().strip('"')
    if not python or not Path(python).is_file():
        return False
    target = root / "pydeps"
    target.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [python, "-m", "pip", "install", "-r", str(root / "requirements.txt"), "-t", str(target)],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
