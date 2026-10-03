@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYCMD="

call :try "%LocalAppData%\Programs\Python\Python313\python.exe"
call :try "%LocalAppData%\Programs\Python\Python312\python.exe"
call :try "%LocalAppData%\Programs\Python\Python311\python.exe"
call :try "%LocalAppData%\Programs\Python\Python310\python.exe"
call :try "C:\Program Files\Python313\python.exe"
call :try "C:\Program Files\Python312\python.exe"
call :try "C:\Program Files\Python311\python.exe"
call :try "C:\Program Files\Python310\python.exe"
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do call :try "%%D\python.exe"
for /d %%D in ("C:\Program Files\Python\Python3*") do call :try "%%D\python.exe"
for /d %%D in ("C:\Python3*") do call :try "%%D\python.exe"
for /d %%D in ("D:\Python3*") do call :try "%%D\python.exe"
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('where python') do call :try "%%P"
)

if not defined PYCMD (
    echo [FAIL] No working Python was found.
    echo D:\Python312\python.exe is registered but cannot start.
    echo Install Python 3.9+ from https://www.python.org/downloads/windows/
    echo Check "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Using Python: %PYCMD%
"%PYCMD%" "%~dp0setup_install.py"
if errorlevel 1 pause
exit /b %errorlevel%

:try
if defined PYCMD goto :eof
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
echo %~1 | find /I "WindowsApps" >nul
if not errorlevel 1 goto :eof
"%~1" -c "import sys; print(sys.executable)" >"%TEMP%\v2-python.txt" 2>nul
if errorlevel 1 goto :eof
set /p PYCMD=<"%TEMP%\v2-python.txt"
goto :eof
