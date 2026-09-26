@echo off
chcp 65001 >nul
title TruePeopleSearch 企业情报中心 - 一键启动器
echo ============================================================
echo   TruePeopleSearch 企业情报中心正在启动...
echo ============================================================

REM 1. 启动 Docker 容器 (如果存在)
docker --version >nul 2>&1
if %errorlevel% equ 0 (
    echo [1/3] 正在启动 TiDB 数据库与 Redis 容器...
    docker compose -f "%~dp0deploy\docker-compose.yml" up -d
)

REM 2. 检查 Python 运行环境
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

REM 3. 启动后台 Supervisor 守护集群与面板
echo [2/3] 正在启动进程守护与面板服务...
start "TPS Supervisor" /min "%PYTHON_EXE%" "%~dp0scripts\tps_supervisor.py" start

REM 4. 等待 2 秒并拉起浏览器
echo [3/3] 服务已就绪，正在打开控制台界面...
timeout /t 2 >nul
start http://127.0.0.1:5001

echo ============================================================
echo   系统已在后台稳定运行！
echo   控制台地址: http://127.0.0.1:5001
echo   如需停止系统，请双击运行 stop_client.bat
echo ============================================================
