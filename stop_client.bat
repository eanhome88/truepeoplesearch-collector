@echo off
chcp 65001 >nul
title TruePeopleSearch 企业情报中心 - 一键停止
echo ============================================================
echo   正在平稳停止 TruePeopleSearch 所有后台服务...
echo ============================================================

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" "%~dp0scripts\tps_supervisor.py" stop

echo ============================================================
echo   所有服务已平稳关闭。
echo ============================================================
pause
