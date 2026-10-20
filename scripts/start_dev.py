"""一键启动本地开发环境：MQTT Broker → 后端 → 虚拟设备运行时 → 大屏。

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
    python scripts/start_dev.py                 # 全套起来（缺哪个补哪个）
    python scripts/start_dev.py --backfill       # 额外先补 24 小时历史数据
    python scripts/start_dev.py --speed 1        # 常驻用真实速度（默认 60 倍速）
    python scripts/start_dev.py --no-web         # 不拉起大屏（只跑后端那套）

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

# 大屏：旧版 frontend/dashboard 已于 2026-10-19 移除，现在只剩 dashboard-v2。
# 端口跟 vite.config.js 的默认保持一致（旧版当年占着 5173，所以 v2 出生在 5174，
# 保持 5174 是为了不让正在开着浏览器的人突然打不开）。
FRONTEND = ROOT / "frontend" / "dashboard-v2"
WEB_PORT = int(os.getenv("VITE_PORT", "5174"))

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


def SCENARIO_REPEAT_FLAG(args: argparse.Namespace) -> list[str]:
    """按参数决定要不要给生成器加 `--scenario-repeat`。

    关掉场景（--scenario none）时不该再传循环开关 —— 那会让人以为"开了循环"，
    实际上一个场景都没有，属于会误导阅读日志的人的配置。
    """
    if args.scenario == "none" or args.no_scenario_repeat:
        return []
    return ["--scenario-repeat"]


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


def _kill(proc: subprocess.Popen) -> None:
    """连子进程一起停。

    ⚠️ Windows 上 `proc.terminate()` 只杀 `npm.cmd` 这一层，它 fork 出来的
    `vite` / `node` 会活下来继续占着端口（下次启动就报"端口被占用"）。
    所以这里用 `taskkill /T` 把整棵进程树一次性收掉。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        proc.terminate()


def start_web() -> subprocess.Popen | None:
    """拉起大屏（`npm run dev`）。

    放在后端之后不是随便排的：dev server 的 `/api` 走 Vite proxy 转发到 8000，
    后端还没起来时页面会一片 502，看起来像前端坏了。
    """
    import shutil

    if not FRONTEND.is_dir():
        print(f"  ⚠️ 找不到大屏目录 {FRONTEND}，跳过")
        return None
    if not (FRONTEND / "node_modules").is_dir():
        print("  ⚠️ 大屏依赖没装（node_modules 不存在），跳过。手动装：")
        print(f"     cd {FRONTEND} && npm install --registry=https://registry.npmmirror.com")
        return None

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        print("  ⚠️ 找不到 npm（不在 PATH 里），跳过拉起大屏")
        return None

    return start("大屏 dev server", [npm, "run", "dev"], FRONTEND, "web.log")


def _ingest_readings(timeout: float = 10.0) -> int | None:
    """读 `/api/health` 里的 `ingest.readings`（后端进程内累计计数）。

    用途：核对"补历史"到底有没有落库。取不到就返回 None（不抛异常，
    因为核对失败本身不该让启动流程挂掉）。
    """
    import json
    import urllib.request

    # ⚠️ 本机可能设了 HTTP_PROXY，会把 127.0.0.1 也走代理 → 取不到。显式禁掉。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8000/api/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return int((data.get("ingest") or {}).get("readings"))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="一键启动：Broker + 后端 + 虚拟设备运行时 + 大屏（缺哪个补哪个）"
    )
    ap.add_argument("--speed", type=float, default=60.0, help="常驻运行的时间倍速（1=真实时间）")
    ap.add_argument("--max-ahead", type=float, default=3.0,
                    help="模拟时间最多领先真实时间多少分钟（防止界面出现未来时间）")
    ap.add_argument("--backfill", action="store_true", help="先补 24 小时历史数据再常驻")
    ap.add_argument("--backfill-minutes", type=float, default=1440.0, help="补多少分钟的历史")
    ap.add_argument(
        "--scenario", default="random",
        help="异常场景：random（默认，循环触发）/ none（只产正常数据）/ 具体场景名。"
             "默认开是有原因的：安防与照护两条业务线只在异常发生时有数据，"
             "关掉之后告警页会一直是空的",
    )
    ap.add_argument(
        "--no-scenario-repeat", action="store_true",
        help="只触发一场异常就停（调试单个场景时用）",
    )
    ap.add_argument("--no-web", action="store_true", help="不拉起大屏（只跑后端那套）")
    ap.add_argument("--no-open", action="store_true", help="起来之后不自动打开浏览器")
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

    # ---- 2. 后端 ----
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

    # ---- 3. 补历史（可选）----
    # ⚠️⚠️ 必须排在后端**之后**。这不是风格问题，是数据会不会丢的问题：
    #     生成器（gateway/）只发 MQTT，**整个目录里没有任何写库代码**；
    #     唯一的入库通道是后端的订阅者（app/mqtt/ingest.py）。
    #     后端没起就补历史 = 消息发进空 MQTT，无人接收，全部丢失。
    #
    #     这个坑很阴：生成器**会正常退出**，还打印"累计发布 N 条"，
    #     光看日志像是成功了，只有去数库里的行数才发现一条都没进去。
    #     2026-10-16 实测：后端未起时补 3 分钟（1155 条），
    #     device_readings 行数 925750 前后完全不变。
    #
    #     下面用 /api/health 的 ingest.readings 做前后核对，
    #     把"悄悄丢数据"变成"当场报错"。
    if args.backfill:
        minutes = int(args.backfill_minutes)
        before = _ingest_readings()
        print(f"\n  补 {minutes} 分钟历史数据（600 倍速，约需几分钟）…")
        subprocess.run(
            [PYTHON, "-m", "gateway.simulator.publisher",
             "--start", f"back:{minutes}", "--duration", str(minutes),
             "--speed", "600", "--progress-every", "480",
             # 历史里也补上异常事件：告警时间轴要有"过去发生过什么"才讲得通
             "--scenario", args.scenario, *SCENARIO_REPEAT_FLAG(args)],
            cwd=str(ROOT), check=False,
        )
        after = _ingest_readings()
        if before is not None and after is not None:
            delta = after - before
            if delta > 0:
                print(f"  ✓ 补历史已落库：readings +{delta:,}（{before:,} → {after:,}）")
            else:
                print(f"  ❌ 补历史**没有落库**：readings {before:,} → {after:,}，增量为 0")
                print("     查 api.log：MQTT 是否 connected、订阅者有没有起来。")
                return 1
        else:
            print("  ⚠️ 无法核对补历史结果（/api/health 取不到 ingest.readings）")

    # ---- 4. 虚拟设备运行时（常驻）----
    # ⚠️ 这一步最关键：没有它，下发指令没人接收，设备状态永远不变。
    print()
    p = start(
        "虚拟设备运行时",
        [PYTHON, "-m", "gateway.simulator.publisher",
         "--start", "auto", "--speed", str(args.speed),
         "--duration", "0", "--max-ahead", str(args.max_ahead),
         "--scenario", args.scenario, *SCENARIO_REPEAT_FLAG(args)],
        ROOT, "simulator.log",
    )
    if p:
        procs.append(p)
    time.sleep(3)

    # ---- 5. 大屏（可选）----
    web_url = f"http://127.0.0.1:{WEB_PORT}"
    if args.no_web:
        print(f"\n  （--no-web：不拉起大屏，自己跑 cd {FRONTEND} && npm run dev）")
    elif port_open("127.0.0.1", WEB_PORT):
        print(f"\n  ✓ 大屏已在运行（{WEB_PORT}）")
    else:
        print()
        p = start_web()
        if p:
            procs.append(p)
        if wait_port(WEB_PORT, 45):
            print(f"  ✓ 大屏就绪（{WEB_PORT}）  → {web_url}")
        else:
            print(f"  ⚠️ 大屏没起来或还在编译，看 web.log（端口 {WEB_PORT}）")

    print()
    print("=" * 70)
    print("全部就绪")
    print(f"  大屏    {web_url}")
    print("  后端    http://127.0.0.1:8000/docs")
    print("  日志    broker.log / api.log / simulator.log"
          + ("" if args.no_web else " / web.log"))
    print()
    print("按 Ctrl+C 停止全部进程")
    print("=" * 70)

    if not args.no_web and not args.no_open and port_open("127.0.0.1", WEB_PORT):
        import webbrowser

        try:
            webbrowser.open(web_url)
        except Exception:  # noqa: BLE001
            pass

    try:
        while True:
            time.sleep(2)
            for proc in procs:
                if proc.poll() is not None:
                    print(f"  ⚠️ 子进程退出：PID {proc.pid}（见对应日志）")
    except KeyboardInterrupt:
        print("\n  正在停止…")
        for proc in procs:
            _kill(proc)
        time.sleep(1)
        for proc in procs:
            if proc.poll() is None:
                _kill(proc)
        print("  已停止（大屏的 vite 进程树也一起收掉了）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
