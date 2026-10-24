@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Khong tim thay Python trong PATH.
  pause
  exit /b 1
)

python -c "import flask, playwright" >nul 2>nul
if errorlevel 1 (
  echo [SETUP] Dang cai thu vien Python...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Khong cai duoc thu vien.
    pause
    exit /b 1
  )
)

echo [START] http://127.0.0.1:5050
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
exit /b %errorlevel%
