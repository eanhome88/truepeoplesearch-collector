@echo off
cd /d "%~dp0"
if not exist "pydeps\httpx\__init__.py" (
    echo Dependencies are missing. Starting install...
    call "%~dp0install.bat"
)
if not exist "pydeps\httpx\__init__.py" (
    echo Install did not finish. Open data\install.log
    pause
    exit /b 1
)
if not exist "python.path" (
    echo python.path is missing. Run install.bat again.
    pause
    exit /b 1
)
if not exist "data" mkdir "data"
if not exist ".env" if exist "..\.env" copy /Y "..\.env" ".env" >nul
set /p PY=<"%~dp0python.path"
for %%I in ("%PY%") do set "PYDIR=%%~dpI"
set "PYW=%PYDIR%pythonw.exe"
if not exist "%PYW%" set "PYW=%PY%"
set "PYTHONPATH=%~dp0pydeps"
start "" "%PYW%" "%~dp0client.pyw"
