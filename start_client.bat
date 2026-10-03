@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title TruePeopleSearch 企业情报中心 - 本机控制台启动器
echo ============================================================
echo   TruePeopleSearch 企业情报中心正在启动本机控制台...
echo ============================================================

REM 1. 检查 Python 运行环境
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] The bundled Python environment is missing. The dashboard was not started.
    pause
    exit /b 1
)
"%PYTHON_EXE%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.9+ environment is unavailable. The dashboard was not started.
    echo         Complete the approved host preparation, then retry.
    pause
    exit /b 1
)
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.9+ is required. The dashboard was not started.
    pause
    exit /b 1
)

REM The supported customer launch path is locally view-only. Strip controls
REM that could otherwise turn inherited host state into outbound work.
set "TPS_RELEASE_MODE=customer"
set "TPS_LOCAL_AUTH_REQUIRED=1"
set "TPS_ALERT_WEBHOOK="
set "TPS_UPDATE_CHECK_URL="
set "PROXY_TUNNEL="
set "PROXY_API="
set "PROXY_FILE="
set "TPS_ALLOW_CLUSTER="
set "TPS_CONCURRENCY="
set "TPS_START_AREA="

for /f "usebackq delims=" %%P in (`"%PYTHON_EXE%" "%~dp0scripts\tps_env.py" --dashboard-port`) do set "TPS_DASHBOARD_PORT=%%P"
if not defined TPS_DASHBOARD_PORT (
    echo [ERROR] The approved dashboard port could not be read. The dashboard was not started.
    pause
    exit /b 1
)
set "TPS_DASHBOARD_URL=http://127.0.0.1:!TPS_DASHBOARD_PORT!"

for /f "usebackq delims=" %%T in (`"%PYTHON_EXE%" -c "import secrets; print(secrets.token_urlsafe(24))"`) do set "TPS_RELEASE_LAUNCH_TOKEN=%%T"
if not defined TPS_RELEASE_LAUNCH_TOKEN (
    echo [ERROR] A local launch identity could not be created. The dashboard was not started.
    pause
    exit /b 1
)

echo [1/3] 不自动启动 Docker、不执行 Git 更新。
echo [2/3] 正在启动本机控制台（不启动后台作业）...
start "TPS Dashboard" /min "%PYTHON_EXE%" "%~dp0scripts\tps_supervisor.py" start --dashboard-only

REM 3. Only open a dashboard which proves it belongs to this exact launch.
set "TPS_DASHBOARD_READY="
for /l %%I in (1,1,12) do (
    if not defined TPS_DASHBOARD_READY (
        "%PYTHON_EXE%" -c "import json, os, sys, urllib.request; token=os.environ['TPS_RELEASE_LAUNCH_TOKEN']; request=urllib.request.Request(os.environ['TPS_DASHBOARD_URL'] + '/api/system/version', headers={'Authorization': 'Bearer ' + token}); response=urllib.request.urlopen(request, timeout=1); payload=json.load(response); sys.exit(0 if payload.get('ok') and payload.get('release_mode') == 'customer' and 'launch_token' not in payload else 1)" >nul 2>&1
        if not errorlevel 1 set "TPS_DASHBOARD_READY=1"
        if not defined TPS_DASHBOARD_READY timeout /t 1 >nul
    )
)
if not defined TPS_DASHBOARD_READY (
    echo [ERROR] The dashboard did not prove this launch identity. The browser was not opened.
    echo         Check the approved host configuration and whether the configured port is already occupied.
    pause
    exit /b 1
)

echo [3/3] 本机控制台已验证，正在打开...
start "" "!TPS_DASHBOARD_URL!/#access_token=!TPS_RELEASE_LAUNCH_TOKEN!"
if errorlevel 1 (
    echo [ERROR] The default browser could not be opened. The client is not ready for use.
    echo         Local address: !TPS_DASHBOARD_URL!
    echo         This address has no authorization token and cannot be used as a login link.
    echo         Configure a default browser, then rerun start_client.bat.
    "%PYTHON_EXE%" "%~dp0scripts\tps_supervisor.py" stop --dashboard-only >nul 2>&1
    pause
    exit /b 1
)

echo ============================================================
echo   本机控制台已验证；请核验 /api/ready 后再进行后续操作。
echo   控制台地址: !TPS_DASHBOARD_URL!
echo   本启动器不会自动启动后台作业或修改 Git 代码。
echo   如需停止本机控制台，请双击运行 stop_client.bat
echo ============================================================
endlocal
