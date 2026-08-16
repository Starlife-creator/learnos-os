@echo off
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title LearnOS - Launcher

echo ============================================
echo   LearnOS - One-Click Launcher
echo ============================================
echo.

rem ---- 1. Check Python ----
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.10+ and check "Add to PATH".
    echo Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem ---- 2. Check Python is runnable and version >= 3.10 ----
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python version too old. 3.10 or newer is required.
    echo Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem ---- 3. Check project files ----
if not exist "app.py" (
    echo [ERROR] app.py not found. Run this script from the project root.
    echo.
    pause
    exit /b 1
)

rem ---- 4. Startup self-check (import core modules + init DB, idempotent) ----
echo [1/3] Running startup self-check...
python -c "import sys; sys.path.insert(0, '.'); import config, db, handler; db.init_db(); print('OK')" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Self-check failed, continuing with degraded mode.
    echo.
)

rem ---- 5. Mode: debug runs in foreground; default runs in background ----
if /i "%~1"=="debug" goto :front
goto :back

rem ---- 5a. Background mode: pythonw, no console window, logs to file ----
:back
where pythonw >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] pythonw not found, falling back to foreground mode.
    goto :front
)
echo [2/3] Starting server in background (no console window)...
echo [3/3] Browser will open automatically: http://127.0.0.1:8765
echo.
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath 'pythonw' -ArgumentList 'app.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
ping -n 4 127.0.0.1 >nul
python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/', timeout=3).status==200 else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Server did not respond. Check logs: learnos.log
    echo.
    pause
    exit /b 1
)
echo Server is running. Logs: learnos.log
echo To stop it, run stop.bat
echo.
ping -n 2 127.0.0.1 >nul
exit /b 0

rem ---- 5b. Foreground mode (debug): keep console for live logs ----
:front
echo [2/3] Starting server (debug mode, Ctrl+C to stop)...
echo [3/3] Browser will open automatically: http://127.0.0.1:8765
echo.
echo Hint: press Ctrl+C to stop, or just close this window.
echo --------------------------------------------
python app.py

set EXIT_CODE=%errorlevel%
echo.
if %EXIT_CODE% neq 0 (
    echo [ERROR] Program exited with code %EXIT_CODE%. See logs above.
) else (
    echo Server stopped.
)
echo.
pause
endlocal