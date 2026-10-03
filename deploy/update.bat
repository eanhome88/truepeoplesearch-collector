@echo off
setlocal
chcp 65001 >nul
title TruePeopleSearch 客户端一键升级 (Windows)
echo ============================================================
echo   TruePeopleSearch 正在检测并拉取云端最新系统更新...
echo ============================================================

cd /d "%~dp0.."
if errorlevel 1 goto update_failed

set "PYTHON_EXE=%cd%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

echo 正在依次检查代码、依赖、数据库及服务重载；任一步失败即中止...
"%PYTHON_EXE%" -c "import sys; from pathlib import Path; sys.path.insert(0, str(Path.cwd() / 'scripts')); import tps_version; res = tps_version.execute_system_update(); [print(l) for l in res.get('logs', [])]; print(res.get('error', '')) if not res.get('ok') else None; sys.exit(0 if res.get('ok') else 1)"
if errorlevel 1 goto update_failed

echo ============================================================
echo 磁盘更新步骤已完成，Windows 服务未自动重启。
echo 请手动重启应用，然后检查控制台服务就绪状态。
echo 请刷新浏览器控制台: http://127.0.0.1:5001
echo ============================================================
pause
exit /b 0

:update_failed
echo ============================================================
echo 升级未完成，已中止后续步骤。请检查以上错误，不要按升级成功处理。
echo ============================================================
pause
exit /b 1
