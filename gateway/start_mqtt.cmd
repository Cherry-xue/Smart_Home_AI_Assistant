@echo off
REM ============================================================
REM 启动 Mosquitto MQTT Broker（本地开发用，前台运行）
REM
REM 本文件只包含 ASCII 路径，可安全放在中文目录下。
REM 停止：在本窗口按 Ctrl+C
REM
REM 安装来源：conda-forge（装在 ai0707 环境内）
REM 配置文件：C:\ProgramData\mosquitto\mosquitto.conf
REM ============================================================

set MOSQ_EXE=D:\soft\anaconda3\envs\ai0707\Library\sbin\mosquitto.exe
set MOSQ_CONF=C:\ProgramData\mosquitto\mosquitto.conf

if not exist "%MOSQ_EXE%" (
    echo [错误] 找不到 mosquitto.exe: %MOSQ_EXE%
    echo 请先执行: conda install -n ai0707 --override-channels -c conda-forge mosquitto -y
    pause
    exit /b 1
)

if not exist "%MOSQ_CONF%" (
    echo [错误] 找不到配置文件: %MOSQ_CONF%
    pause
    exit /b 1
)

echo 正在启动 Mosquitto（127.0.0.1:1883）...
echo 配置文件: %MOSQ_CONF%
echo 停止请按 Ctrl+C
echo.

"%MOSQ_EXE%" -c "%MOSQ_CONF%" -v
