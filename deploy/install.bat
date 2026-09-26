@echo off
chcp 65001 >nul
title TruePeopleSearch 客户端一键安装向导 (Windows)
echo ============================================================
echo   TruePeopleSearch 客户端一键安装向导 (Windows)
echo ============================================================

REM 1. 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9+ 并勾选 "Add Python to PATH"。
    pause
    exit /b 1
)
echo [1/4] Python 环境检查通过。

REM 2. 创建虚拟环境
if not exist "%~dp0..\.venv" (
    echo [2/4] 正在创建 Python 虚拟环境 (.venv)...
    python -m venv "%~dp0..\.venv"
)
set "VENV_PYTHON=%~dp0..\.venv\Scripts\python.exe"
set "VENV_PIP=%~dp0..\.venv\Scripts\pip.exe"

REM 3. 安装依赖
echo [3/4] 正在安装与同步依赖包...
"%VENV_PIP%" install -r "%~dp0..\requirements.txt" --quiet

REM 4. 检查 Docker 并启动 TiDB + Redis
echo [4/4] 检查本地数据库 (TiDB) 与消息队列 (Redis)...
docker --version >nul 2>&1
if %errorlevel% equ 0 (
    echo 发现 Docker Desktop，正在启动本地 TiDB 与 Redis 容器...
    docker compose -f "%~dp0docker-compose.yml" up -d
    timeout /t 5 >nul
) else (
    echo [提示] 未检测到 Docker Desktop，请确保本地已运行 TiDB 或 MySQL(4000/3306端口) 及 Redis(6379端口)。
)

REM 5. 初始化数据库表结构
"%VENV_PYTHON%" "%~dp0init_db.py"

echo ============================================================
echo 🎉 客户端初始化安装完成！
echo 双击运行 start_client.bat 即可一键启动系统并打开控制台！
echo ============================================================
pause
