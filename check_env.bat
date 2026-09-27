@echo off
chcp 65001 >nul
title TruePeopleSearch 客户端环境一键体检与安装向导 (Windows)

echo ============================================================
echo   TruePeopleSearch 客户端环境一键体检与安装向导 (Windows)
echo ============================================================

REM 1. 检查 Python 运行环境
echo [*] 正在检测 Python 运行环境...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [❌ 未检测到 Python]
    echo.
    echo   👉 极速自动安装方案 (推荐，直接在 CMD 运行):
    echo      winget install Python.Python.3.11
    echo.
    echo   👉 或手动下载官方安装包:
    echo      https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    echo   ⚠️ 重要提示: 安装时请务必勾选底部的 [Add python.exe to PATH]！
    echo ------------------------------------------------------------
) else (
    for /f "tokens=*" %%i in ('python --version') do echo [✅ Python 已安装] %%i
)

REM 2. 检查 Git 工具 (云端更新必备)
echo [*] 正在检测 Git 工具...
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [❌ 未检测到 Git]
    echo.
    echo   👉 极速自动安装方案 (在 CMD 运行):
    echo      winget install Git.Git
    echo.
    echo   👉 或手动下载官方安装包:
    echo      https://github.com/git-for-windows/git/releases/download/v2.44.0.windows.1/Git-2.44.0-64-bit.exe
    echo ------------------------------------------------------------
) else (
    for /f "tokens=*" %%i in ('git --version') do echo [✅ Git 已安装] %%i
)

REM 3. 检查 Docker Desktop (本地数据库容器)
echo [*] 正在检测 Docker 容器引擎...
docker --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ℹ️ 未检测到 Docker Desktop]
    echo.
    echo   👉 本地数据库建议方案 (二选一):
    echo      方案 A (推荐): 安装 Docker Desktop (全自动拉起 TiDB 与 Redis)
    echo             下载: https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe
    echo             或命令: winget install Docker.DockerDesktop
    echo.
    echo      方案 B: 本地自装 MySQL (端口 4000/3306) 与 Redis (端口 6379)
    echo ------------------------------------------------------------
) else (
    for /f "tokens=*" %%i in ('docker --version') do echo [✅ Docker 已安装] %%i
)

echo.
echo [*] 正在调用深度诊断程序分析端口与依赖库...
echo.

REM 4. 尝试通过 Python 进行深度检测
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" "%~dp0check_env.py"

echo ============================================================
echo   体检完毕！
echo   • 若各项全部绿色通过，请双击运行【TruePeopleSearch.exe】启动系统！
echo   • 若首次使用或依赖缺失，请先双击运行【deploy\install.bat】完成配置。
echo ============================================================
pause
