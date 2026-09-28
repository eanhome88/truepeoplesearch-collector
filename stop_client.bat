@echo off
chcp 65001 >nul
title TruePeopleSearch 企业情报中心 - 一键停止
echo ============================================================
echo   正在停止本次客户控制台启动的服务...
echo ============================================================

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" "%~dp0scripts\tps_supervisor.py" stop --dashboard-only
if errorlevel 1 (
    echo ============================================================
    echo   未停止任何服务：目标不是本次客户控制台启动的实例。
    echo ============================================================
    pause
    exit /b 1
)

echo ============================================================
echo   客户控制台停止请求已完成；不会停止未标记为客户控制台的服务。
echo ============================================================
pause
