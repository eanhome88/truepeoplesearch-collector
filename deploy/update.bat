@echo off
chcp 65001 >nul
title TruePeopleSearch 客户端一键升级 (Windows)
echo ============================================================
echo   TruePeopleSearch 正在检测并拉取云端最新系统更新...
echo ============================================================

set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" -c "import sys; sys.path.insert(0, r'%~dp0..\scripts'); import tps_version; res = tps_version.execute_system_update(force_stash=True); [print(l) for l in res.get('logs', [])]; sys.exit(0 if res.get('ok') else 1)"

if %errorlevel% equ 0 (
    echo ============================================================
    echo 🎉 升级完成！请刷新浏览器控制台: http://127.0.0.1:5001
    echo ============================================================
) else (
    echo [错误] 升级过程遇到问题，请检查网络或 Git 配置。
)
pause
