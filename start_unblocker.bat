@echo off
chcp 65001 >nul
title TruePeopleSearch 自建私有解盾 API 网关

echo ============================================================
echo   启动 TruePeopleSearch 自建私有解盾 API 网关
echo   服务监听地址: http://127.0.0.1:8088
echo ============================================================

set ROOT=%~dp0
set PYTHON=%ROOT%\.venv\Scripts\python.exe

if not exist "%PYTHON%" (
    set PYTHON=python
)

"%PYTHON%" "%ROOT%\tools\local_unblocker_api.py" --host 127.0.0.1 --port 8088

pause
