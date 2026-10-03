@echo off
setlocal EnableExtensions
chcp 65001 >nul
title TPS 数据库连接检测

echo ============================================================
echo   TruePeopleSearch 数据库状态检测
echo ============================================================

set "PYTHON_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "D:\caiji\.venv\Scripts\python.exe" set "PYTHON_EXE=D:\caiji\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%~dp0..\.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%~dp0Python312\python.exe" set "PYTHON_EXE=%~dp0Python312\python.exe"
if not defined PYTHON_EXE if exist "D:\caiji\Python312\python.exe" set "PYTHON_EXE=D:\caiji\Python312\python.exe"

if not defined PYTHON_EXE (
    echo [ERROR] 未找到 Python 环境，请确保脚本位于 D:\caiji\ 目录下运行！
    echo 当前目录: %~dp0
    pause
    exit /b 1
)

set "SCRIPT_FILE="
if exist "%~dp0tools\fix_db_now.py" set "SCRIPT_FILE=%~dp0tools\fix_db_now.py"
if not defined SCRIPT_FILE if exist "D:\caiji\tools\fix_db_now.py" set "SCRIPT_FILE=D:\caiji\tools\fix_db_now.py"
if not defined SCRIPT_FILE if exist "%~dp0..\tools\fix_db_now.py" set "SCRIPT_FILE=%~dp0..\tools\fix_db_now.py"

if not defined SCRIPT_FILE (
    echo [ERROR] 未找到 tools\fix_db_now.py 文件！
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_FILE%"

echo ============================================================
pause
