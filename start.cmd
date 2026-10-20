@echo off
REM ============================================================
REM  Smart Home AI - one-click launcher (Windows)
REM  Content is ASCII-only on purpose: cmd.exe reads .cmd in the
REM  local code page, non-ASCII bytes would be mangled.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY=D:\soft\anaconda3\envs\ai0707\python.exe"

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    echo         Edit PY in start.cmd to point at your conda python.exe
    pause
    exit /b 1
)

echo Starting: MQTT Broker -^> Backend -^> Simulator -^> Dashboard
echo (Ctrl+C in this window stops everything)
echo.

REM Double-click = full stack (with 24h backfill).
REM Pass args to override, e.g:  start.cmd --web          (skip backfill)
REM                              start.cmd --no-web       (backend only)
if "%~1"=="" (
    "%PY%" scripts\start_dev.py --backfill --web
) else (
    "%PY%" scripts\start_dev.py %*
)

echo.
echo Stopped.
pause
