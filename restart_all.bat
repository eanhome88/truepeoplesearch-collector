@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "STACK_ROOT=D:\TruePeopleSearch\app\deploy\windows-full"
if not exist "%STACK_ROOT%\Stop-Stack.ps1" exit /b 1
if not exist "%STACK_ROOT%\Start-Stack.ps1" exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%STACK_ROOT%\Stop-Stack.ps1"
if errorlevel 1 exit /b %errorlevel%
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%STACK_ROOT%\Start-Stack.ps1"
exit /b %errorlevel%
