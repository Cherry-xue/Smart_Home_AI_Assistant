"""一键启动本地开发环境：MQTT Broker → 后端 → 虚拟设备运行时。

为什么需要这个脚本
------------------
这三样必须**按这个顺序**起来，缺一个就会出现很迷惑的现象：

| 缺了谁 | 表现 |
|---|---|
| Broker | 后端起不来订阅；`health.mqtt.connected=false`，模拟器连不上退出 |
| 后端 | 页面 502；MQTT 数据没人落库 |
| **虚拟设备运行时** | **下发指令"成功"但设备状态纹丝不动** ← 最容易误判为 bug |

尤其是第三条：模拟器是独立进程，跑完批量历史就退出，之后没人接收控制指令。
所以正确用法是"先补历史，再常驻实时运行"——本脚本负责的就是这个。

用法
----
    python scripts/start_dev.py                 # 三个都起来（缺哪个补哪个）
    python scripts/start_dev.py --backfill      # 额外先补 24 小时历史数据
    python scripts/start_dev.py --speed 1       # 常驻用真实速度（默认 60 倍速）

⚠️ Mosquitto 不是 Windows 服务，每次开机都要重新起 —— 所以才有这个脚本。
   按 Ctrl+C 可以一次停掉全部子进程。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

# Mosquitto 是 conda 装的（不在 PATH 里），路径按本机实测写死，可用环境变量覆盖
MOSQUITTO = os.getenv(
    "MOSQUITTO_EXE",
    r"D:\soft\anaconda3\envs\ai0707\Library\sbin\mosquitto.exe",
)
MOSQUITTO_CONF = os.getenv("MOSQUITTO_CONF", r"C:\ProgramData\mosquitto\mosquitto.conf")

PYTHON = sys.executable


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port(port: int, seconds: float = 30.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open("127.0.0.1", port):
            return True
        time.sleep(0.5)
    return False


def start(name: str, args: list[str], cwd: Path, log_name: str) -> subprocess.Popen | None:
    log_path = ROOT / log_name
    fp = open(log_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=fp,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    print(f"  ▶ {name} 已启动（PID {proc.pid}），日志：{log_path.name}")
    return proc


def main() -> int:
    ap = argparse.ArgumentParser(description="一键启动：Broker + 后端 + 虚拟设备运行时")
    ap.add_argument("--speed", type=float, default=60.0, help="常驻运行的时间倍速（1=真实时间）")
    ap.add_argument("--max-ahead", type=float, default=3.0,
                    help="模拟时间最多领先真实时间多少分钟（防止界面出现未来时间）")
    ap.add_argument("--backfill", action="store_true", help="先补 24 小时历史数据再常驻")
    ap.add_argument("--backfill-minutes", type=float, default=1440.0, help="补多少分钟的历史")
    args = ap.parse_args()

    procs: list[subprocess.Popen] = []

    print("=" * 70)
    print("智能家居 AI 助手 · 本地开发环境启动")
    print("=" * 70)

    # ---- 1. Broker ----
    if port_open("127.0.0.1", 1883):
        print("  ✓ MQTT Broker 已在运行（1883）")
    else:
        if not Path(MOSQUITTO).exists():
            print(f"  ❌ 找不到 mosquitto：{MOSQUITTO}")
            print("     可用环境变量 MOSQUITTO_EXE 指定路径")
            return 1
        p = start("MQTT Broker", [MOSQUITTO, "-c", MOSQUITTO_CONF, "-v"], ROOT, "broker.log")
        if p:
            procs.append(p)
        if not wait_port(1883, 15):
            print("  ❌ Broker 起不来，看 broker.log")
            return 1
        print("  ✓ MQTT Broker 就绪（1883）")

    # ---- 2. 补历史（可选）----
    if args.backfill:
        minutes = int(args.backfill_minutes)
        print(f"\n  补 {minutes} 分钟历史数据（600 倍速，约需几分钟）…")
        subprocess.run(
            [PYTHON, "-m", "gateway.simulator.publisher",
             "--start", f"back:{minutes}", "--duration", str(minutes),
             "--speed", "600", "--progress-every", "480"],
            cwd=str(ROOT), check=False,
        )

    # ---- 3. 后端 ----
    if port_open("127.0.0.1", 8000):
        print("  ✓ 后端已在运行（8000）")
    else:
        p = start("后端 API", [PYTHON, "run_api.py", "--no-reload", "--port", "8000"],
                  BACKEND, "api.log")
        if p:
            procs.append(p)
        if not wait_port(8000, 40):
            print("  ❌ 后端起不来，看 api.log")
            return 1
        print("  ✓ 后端就绪（8000）  → http://127.0.0.1:8000/docs")

    # ---- 4. 虚拟设备运行时（常驻）----
    # ⚠️ 这一步最关键：没有它，下发指令没人接收，设备状态永远不变。
    print()
    p = start(
        "虚拟设备运行时",
        [PYTHON, "-m", "gateway.simulator.publisher",
         "--start", "auto", "--speed", str(args.speed),
         "--duration", "0", "--max-ahead", str(args.max_ahead)],
        ROOT, "simulator.log",
    )
    if p:
        procs.append(p)
    time.sleep(3)

    print()
    print("=" * 70)
    print("全部就绪")
    print("  大屏    http://127.0.0.1:5173   （需另开终端跑 npm run dev）")
    print("  后端    http://127.0.0.1:8000/docs")
    print("  日志    broker.log / api.log / simulator.log")
    print()
    print("按 Ctrl+C 停止全部进程")
    print("=" * 70)

    try:
        while True:
            time.sleep(2)
            for proc in procs:
                if proc.poll() is not None:
                    print(f"  ⚠️ 子进程退出：PID {proc.pid}（见对应日志）")
    except KeyboardInterrupt:
        print("\n  正在停止…")
        for proc in procs:
            proc.terminate()
        time.sleep(1)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        print("  已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
