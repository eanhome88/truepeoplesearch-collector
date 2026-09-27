@echo off
cd /d "%~dp0"
set "LOG=%~dp0install-log.txt"
echo ==== %date% %time% ==== > "%LOG%"
echo install dir: %~dp0>> "%LOG%"
title TPS install
echo.
echo TPS install starting. This window stays open.
echo Log: %LOG%
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3.9+ and tick Add Python to PATH.
  echo Python not found>> "%LOG%"
  pause
  exit /b 1
)
echo [1/6] Python OK
python --version
python --version >> "%LOG%"

if not exist "%~dp0..\.venv\Scripts\python.exe" (
  echo [2/6] Creating .venv
  python -m venv "%~dp0..\.venv"
  if errorlevel 1 (
    echo venv failed>> "%LOG%"
    echo Failed to create .venv
    pause
    exit /b 1
  )
) else (
  echo [2/6] .venv already exists
)
set "VENV_PYTHON=%~dp0..\.venv\Scripts\python.exe"
set "VENV_PIP=%~dp0..\.venv\Scripts\pip.exe"
if not exist "%VENV_PYTHON%" (
  echo venv python missing: %VENV_PYTHON%
  echo missing venv python>> "%LOG%"
  pause
  exit /b 1
)

echo [3/6] Installing requirements
"%VENV_PIP%" install -r "%~dp0..\requirements.txt"
if errorlevel 1 (
  echo pip failed>> "%LOG%"
  echo pip install failed. See messages above.
  pause
  exit /b 1
)

echo [4/6] Installing browser
"%VENV_PYTHON%" -m patchright install chromium
echo patchright exit %errorlevel%>> "%LOG%"

echo [5/6] Database
where docker >nul 2>&1
if errorlevel 1 (
  echo Docker not found. Need TiDB or MySQL on port 4000 or 3306, and Redis on 6379.
  echo no docker>> "%LOG%"
) else (
  echo Starting docker compose
  docker compose -f "%~dp0docker-compose.yml" up -d
  timeout /t 5 /nobreak >nul
)

echo [6/6] Init tables
"%VENV_PYTHON%" "%~dp0init_db.py"
echo init_db exit %errorlevel%>> "%LOG%"
if errorlevel 1 (
  echo Database init failed. See messages above.
  pause
  exit /b 1
)

echo.
echo Done. Next: double-click start_client.bat in the parent folder.
echo done>> "%LOG%"
pause
