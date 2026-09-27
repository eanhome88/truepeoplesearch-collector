@echo off
chcp 65001 >nul
title TruePeopleSearch 客户端一键升级 (Windows)
echo ============================================================
echo   TruePeopleSearch 正在检测并拉取云端最新系统更新...
echo ============================================================

cd /d "%~dp0.."

set "PYTHON_EXE=%cd%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

echo [1/3] 正在拉取云端最新代码...
git pull

echo [2/3] 正在同步数据库表结构与人物主表视图...
"%PYTHON_EXE%" deploy\init_db.py

echo [3/3] 正在校验系统依赖与平滑重载服务...
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0, r'%cd%\scripts'); import tps_version; res = tps_version.execute_system_update(force_stash=True); [print(l) for l in res.get('logs', [])]"

echo ============================================================
echo 🎉 升级完成！数据库结构与采集引擎已更新完毕。
echo 请刷新浏览器控制台: http://127.0.0.1:5001
echo ============================================================
pause
