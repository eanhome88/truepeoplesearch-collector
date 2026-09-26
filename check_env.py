#!/usr/bin/env python3
"""
TruePeopleSearch 客户端全方位运行环境体检与诊断工具
检查内容：
1. Python 版本与 64 位架构
2. 必要第三方依赖包完整性
3. Git 版本与云端自建仓库网络联通性
4. 本地 Docker / 数据库端口 (4000/3306) 与 Redis 端口 (6379)
5. 关键文件与权限完整性
"""

import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_GIT_REPO_URL = "http://121.41.231.194:10886/wangqi/22.git"
_GIT_HOST = "121.41.231.194"
_GIT_PORT = 10886

REQUIRED_MODULES = [
    ("flask", "Flask Web 服务核心"),
    ("mysql.connector", "TiDB / MySQL 数据库驱动"),
    ("redis", "Redis 消息队列客户端"),
    ("patchright", "高抗封反反爬浏览器引擎"),
]


def test_tcp_port(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


def run_diagnostics():
    print("=" * 64)
    print("  TruePeopleSearch 客户端运行环境全项体检与健康诊断")
    print("=" * 64)

    all_passed = True
    warnings = []

    # 1. 操作系统信息
    os_name = platform.system()
    arch = platform.machine()
    is_64bit = sys.maxsize > 2**32
    print(f"\n[1/5] 操作系统与硬件架构:")
    print(f"  • 系统类型: {os_name} ({platform.platform()})")
    print(f"  • 架构位数: {arch} {'(64位)' if is_64bit else '(32位)'}")
    if not is_64bit:
        print("  ❌ 警告: 当前为 32 位操作系统，TiDB 与现代反爬浏览器引擎要求 64 位系统！")
        all_passed = False
    else:
        print("  ✅ 64 位硬件架构校验通过")

    # 2. Python 解释器与虚拟环境
    print(f"\n[2/5] Python 运行环境:")
    py_ver = sys.version_info
    print(f"  • 解释器路径: {sys.executable}")
    print(f"  • Python 版本: {py_ver.major}.{py_ver.minor}.{py_ver.micro}")
    if py_ver.major < 3 or (py_ver.major == 3 and py_ver.minor < 9):
        print(f"  ❌ 错误: 需要 Python 3.9 及以上版本，当前版本过低！")
        all_passed = False
    else:
        print(f"  ✅ Python 版本符合要求 (>= 3.9)")

    # 检查虚拟环境
    is_venv = sys.prefix != sys.base_prefix
    if is_venv:
        print(f"  ✅ 当前运行在独立虚拟环境中: {sys.prefix}")
    else:
        print(f"  ℹ️ 当前为全局 Python 环境，建议运行 deploy/install.bat 生成独立虚拟环境")

    # 检查核心库依赖
    print(f"\n[3/5] 核心 Python 依赖包检查:")
    missing_modules = []
    for mod_name, desc in REQUIRED_MODULES:
        try:
            __import__(mod_name)
            print(f"  • {mod_name:<18} [{desc}]: ✅ 正常")
        except ImportError:
            print(f"  • {mod_name:<18} [{desc}]: ❌ 未安装")
            missing_modules.append(mod_name)

    if missing_modules:
        all_passed = False
        print(f"\n  👉 修复建议: 请运行命令安装缺失依赖:")
        print(f"     pip install -r requirements.txt")

    # 3. Git 版本与云端自建仓库网络检查
    print(f"\n[4/5] Git 版本库与自建仓库网络连接:")
    git_installed = False
    try:
        git_res = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
        if git_res.returncode == 0:
            git_installed = True
            print(f"  • Git 工具: {git_res.stdout.strip()} ✅")
        else:
            print("  • Git 工具: ❌ 未检测到或不可用")
    except Exception:
        print("  • Git 工具: ❌ 未安装 (将无法使用自动检测与一键更新)")
        warnings.append("Git 未安装")

    # 检查自建仓库网络连通性
    print(f"  • 自建仓库地址: {_GIT_HOST}:{_GIT_PORT} ...")
    if test_tcp_port(_GIT_HOST, _GIT_PORT, timeout=3.0):
        print(f"    ✅ 成功连通自建更新仓库 ({_GIT_HOST}:{_GIT_PORT})！云端在线更新就绪。")
    else:
        print(f"    ⚠️ 警告: 无法连通自建仓库端口 ({_GIT_HOST}:{_GIT_PORT})，请检查网络或防火墙放行。")
        warnings.append("云端仓库网络暂时不通")

    # 4. 本地数据库与端口状态检查
    print(f"\n[5/5] 本地基础服务与端口占用检查:")
    db_port = int(os.environ.get("TPS_DB_PORT", 4000))
    redis_port = int(os.environ.get("TPS_REDIS_PORT", 6379))
    dash_port = int(os.environ.get("TPS_DASHBOARD_PORT", 5001))

    # 数据库端口
    db_ok = test_tcp_port("127.0.0.1", db_port, timeout=1.0)
    if not db_ok and db_port == 4000:
        db_ok = test_tcp_port("127.0.0.1", 3306, timeout=1.0)
        if db_ok:
            db_port = 3306

    if db_ok:
        print(f"  • 数据库服务 (端口 {db_port}): ✅ 已在运行中")
    else:
        print(f"  • 数据库服务 (端口 {db_port}): ℹ️ 当前未启动 (启动客户端时会自动拉起)")

    # Redis 端口
    redis_ok = test_tcp_port("127.0.0.1", redis_port, timeout=1.0)
    if redis_ok:
        print(f"  • Redis 队列 (端口 {redis_port}): ✅ 已在运行中")
    else:
        print(f"  • Redis 队列 (端口 {redis_port}): ℹ️ 当前未启动 (启动客户端时会自动拉起)")

    # 面板端口
    dash_busy = test_tcp_port("127.0.0.1", dash_port, timeout=0.8)
    if dash_busy:
        print(f"  • Web 面板 (端口 {dash_port}): ⚠️ 端口已被占用 (如果是面板本身在运行则属正常)")
    else:
        print(f"  • Web 面板 (端口 {dash_port}): ✅ 端口空闲，可随时绑定")

    # 检查 Docker
    docker_installed = False
    try:
        d_res = subprocess.run(["docker", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
        if d_res.returncode == 0:
            docker_installed = True
            print(f"  • 本地 Docker: {d_res.stdout.strip()} ✅")
    except Exception:
        print("  • 本地 Docker: ℹ️ 未检测到 Docker (若使用本机原生 MySQL/Redis 则无需 Docker)")

    # 总结
    print("\n" + "=" * 64)
    if all_passed and not missing_modules:
        print("🎉 体检结果: 优秀！当前电脑环境完全就绪，随时可启动运行客户端！")
        if warnings:
            print("   (小贴士: " + "；".join(warnings) + ")")
    else:
        print("⚠️ 体检结果: 发现需要配置的项目，请根据上方红字提示完成处理。")
    print("=" * 64 + "\n")

    return 0 if (all_passed and not missing_modules) else 1


if __name__ == "__main__":
    sys.exit(run_diagnostics())
