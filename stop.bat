@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Physics Study OS - Stop Server

rem Find PID listening on 8765 and kill only that process
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%p >nul 2>&1 && set FOUND=1
)

if "%FOUND%"=="1" (
    echo Server stopped.
) else (
    echo No server is running on port 8765.
)
ping -n 2 127.0.0.1 >nul
exit /b 0