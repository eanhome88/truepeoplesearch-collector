@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title TPS MariaDB 数据库管理

echo ============================================================
echo   TruePeopleSearch 本地数据库启动向导 (端口 3306)
echo ============================================================

REM 1. 尝试启动已注册的 Windows 服务
echo [*] 正在检查系统服务...
net start MariaDB >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] MariaDB 系统服务已成功启动！
    goto :check_running
)
net start MySQL >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] MySQL 系统服务已成功启动！
    goto :check_running
)

REM 2. 先清理可能残留的僵死 mysqld 进程
taskkill /F /IM mysqld.exe >nul 2>&1

REM 3. 全盘搜索 mysqld.exe 真实路径 (无视任何嵌套目录结构)
echo [*] 正在检索 mysqld.exe 真实文件位置...
set "MYSQLD_EXE="
for /f "delims=" %%i in ('dir /s /b D:\caiji\mysqld.exe 2^>nul') do (
    if not defined MYSQLD_EXE set "MYSQLD_EXE=%%i"
)

if not defined MYSQLD_EXE (
    if exist "D:\caiji\mariadb.zip" (
        echo [*] 正在自动解压 D:\caiji\mariadb.zip...
        powershell -NoProfile -Command "Expand-Archive -Path 'D:\caiji\mariadb.zip' -DestinationPath 'D:\caiji\' -Force"
        for /f "delims=" %%i in ('dir /s /b D:\caiji\mysqld.exe 2^>nul') do (
            if not defined MYSQLD_EXE set "MYSQLD_EXE=%%i"
        )
    )
)

if not defined MYSQLD_EXE (
    echo [ERROR] 在 D:\caiji\ 下未检索到 mysqld.exe！
    echo 请确认 mariadb.zip 是否已完整解压。
    pause
    exit /b 1
)

echo [*] 成功定位数据库核心: "!MYSQLD_EXE!"

REM 4. 自动推导 BASEDIR 与 DATADIR
for %%a in ("!MYSQLD_EXE!\..\..") do set "BASEDIR=%%~fa"
set "DATADIR=!BASEDIR!\data"

echo [*] MariaDB 根目录: "!BASEDIR!"
echo [*] 数据存储目录: "!DATADIR!"

if not exist "!DATADIR!" mkdir "!DATADIR!" >nul 2>&1
attrib -r -s "!DATADIR!\*.*" /s >nul 2>&1

REM 5. 切换到 MariaDB 目录下以原生环境启动
cd /d "!BASEDIR!"
echo [*] 正在拉起 MariaDB 服务监听 3306 端口...
start "TPS-MariaDB" "!MYSQLD_EXE!" --console --basedir="!BASEDIR!" --datadir="!DATADIR!" --port=3306

:check_running
echo.
echo [*] 正在等待 3 秒验证数据库连接...
timeout /t 3 >nul

if exist "D:\caiji\.venv\Scripts\python.exe" if exist "D:\caiji\tools\fix_db_now.py" (
    echo ============================================================
    D:\caiji\.venv\Scripts\python.exe D:\caiji\tools\fix_db_now.py
)

echo ============================================================
pause
