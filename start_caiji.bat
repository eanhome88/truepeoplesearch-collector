@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title TruePeopleSearch 生产环境采集集群

echo ============================================================
echo   TruePeopleSearch 生产环境采集集群正在启动...
echo ============================================================

REM 1. 检查 Python 运行环境
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] 未找到虚拟环境: %PYTHON_EXE%
    pause
    exit /b 1
)

REM 2. 设置穿云动态住宅代理与穿云 API 环境变量
REM 代理只从本机 config/runtime.env 读取，不要写进仓库。
set "PROXY_TUNNEL="
set "REDIS_HOST=127.0.0.1"
set "REDIS_PORT=6379"
set "TPS_REDIS_HOST=127.0.0.1"
set "TPS_REDIS_PORT=6379"
set "TPS_DB_HOST=127.0.0.1"
set "TPS_DB_PORT=3306"
set "TPS_DB_USER=root"
set "TPS_DB_PASSWORD=tps123456"
set "TPS_DB_NAME=people_search"
set "CLOUDBYPASS_APIKEY=4de045d19fdc477f8abfc323a15c7f9e"
set "CLOUDBYPASS_PROXY="
set "CLOUDBYPASS_SITEKEY=0x4AAAAAAAmywfqBst8n7ro5"
set "USE_CLOUDBYPASS=1"
set "TPS_RELEASE_MODE=standard"
set "NO_RATE_LIMIT_COOLDOWN=1"
set "RATE_LIMIT_PAUSE_SEC=0"
set "TPS_NO_COOLDOWN=1"

del /f /q data\supervisor.pid* data\supervisor.lock >nul 2>&1

echo [1/3] 启动控制大屏 (端口 5001)...
start "TPS-1-控制大屏" cmd /k "cd /d %~dp0 && .venv\Scripts\python.exe tools\dashboard_api.py --port 5001"

echo [2/3] 启动全美电话发生器 (区号 201 起)...
start "TPS-2-电话发生器" cmd /k "cd /d %~dp0 && .venv\Scripts\python.exe scripts\phone_discover.py --start-area 201"

echo [3/3] 启动穿云高性能分布式采集 Worker (并发 10)...
start "TPS-3-采集Worker" cmd /k "cd /d %~dp0 && .venv\Scripts\python.exe scripts\distributed_worker.py --mode worker --concurrency 10"

timeout /t 3 >nul
start http://127.0.0.1:5001/#/recent

echo ============================================================
echo   全部采集服务已启动！
echo   控制大屏: http://127.0.0.1:5001
echo ============================================================
pause
