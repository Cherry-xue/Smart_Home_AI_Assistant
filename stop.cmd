@echo off
REM ============================================================
REM  Smart Home AI - one-click stopper (Windows)
REM  ASCII-only on purpose: cmd.exe reads .cmd in the local code page.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY=D:\soft\anaconda3\envs\ai0707\python.exe"

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    echo         Edit PY in stop.cmd to point at your conda python.exe
    pause
    exit /b 1
)

echo Stopping: MQTT Broker / Backend / Simulator / Dashboard
echo.

REM Double-click = stop everything.
REM Pass args to override, e.g:  stop.cmd --dry-run   (list only, kill nothing)
if "%~1"=="" (
    "%PY%" scripts\stop_dev.py
) else (
    "%PY%" scripts\stop_dev.py %*
)

echo.
pause
