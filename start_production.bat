@echo off
setlocal EnableExtensions
chcp 65001 >nul
if exist "%~dp0start_caiji.bat" (
    call "%~dp0start_caiji.bat" %*
    exit /b %errorlevel%
)
set "STACK_ROOT=D:\TruePeopleSearch\app\deploy\windows-full"
if not exist "%STACK_ROOT%\Start-Stack.ps1" (
    echo [ERROR] Verified D: runtime is not installed. No service was started.
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%STACK_ROOT%\Start-Stack.ps1"
exit /b %errorlevel%
