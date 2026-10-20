"""一键停掉本地开发环境（start_dev.py 的反操作）。

为什么不能只按端口杀
--------------------
这套环境里有**两类进程**：

| 进程 | 有没有端口 | 怎么找 |
|---|---|---|
| Mosquitto Broker | 1883 | 端口 |
| 后端 API | 8000 | 端口 |
| 大屏 dev server | 5174 | 端口 |
| **虚拟设备运行时** | **没有端口** | **只能靠命令行** ← 只按端口杀会漏掉它 |

漏掉模拟器的后果不是"少关一个"，而是下次启动会变成**两个模拟器同时发数据**，
时间线直接错乱（数据点重叠、能耗增量翻倍）。所以这里两类一起收：
先按端口找，再按命令行关键字找（`gateway.simulator.publisher`）。

用法
----
    python scripts/stop_dev.py             # 全停
    python scripts/stop_dev.py --dry-run   # 只看会杀谁，不动手
    python scripts/stop_dev.py --keep-db   # 只停环境，别的不管（占位，目前同全停）

⚠️ Windows 上必须 `taskkill /T`：`terminate()` 只杀父进程，vite / node 会活着占端口。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 按端口找：本地开发环境固定占用的几个
PORTS = [1883, 8000, int(os.getenv("VITE_PORT", "5174"))]

#: 按命令行关键字找（覆盖没有端口的进程，以及 npm/vite 这种多层的）
CMD_KEYWORDS = [
    "gateway.simulator.publisher",   # 虚拟设备运行时（无端口，最关键）
    "run_api.py",                    # 后端
    "mosquitto",                     # Broker
    "dashboard-v2",                  # 大屏（vite / esbuild 都在项目目录下）
    "vite.js",                       # 兜底：任何 vite dev server
    "npm-cli.js",                    # 大屏的 npm run dev 外壳（杀了它才不会留一个空壳）
]

#: 这些进程虽然命令行里带上了关键字，但**绝不能杀**：
#: 查询命令本身是 `powershell ... -like '*gateway.simulator.publisher*'`，
#: 它自己当然匹配自己 —— 不排除就会把自己刚启动的查询进程打死（还会连累别的 powershell）。
CMD_EXCLUDE = ["Get-CimInstance", "powershell"]


def pids_by_port(port: int) -> list[int]:
    """从 netstat 里找出 LISTENING 在该端口的 PID。"""
    try:
        # ⚠️ Windows 的 netstat 输出是**本地代码页**（中文机 = GBK），
        #    按 UTF-8 解码会直接抛 UnicodeDecodeError 把脚本打死。errors=replace 兜住。
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, errors="replace", timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        # 形如： TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING    1076
        m = re.search(r"[:\.](\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", line.strip())
        if not m:
            continue
        if int(m.group(1)) == port:
            pid = int(m.group(2))
            if pid not in pids:
                pids.append(pid)
    return pids


def pids_by_command(keywords: list[str]) -> dict[int, str]:
    """用 PowerShell 按命令行关键字找进程（wmic 在本机被安全策略拦了，不能走那条路）。"""
    cond = " -or ".join(f"$_.CommandLine -like '*{k}*'" for k in keywords)
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ {cond} }} | "
        "Select-Object ProcessId,CommandLine | "
        "ForEach-Object { \"{0}|{1}\" -f $_.ProcessId, $_.CommandLine }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, errors="replace", timeout=60,
        ).stdout
    except Exception:  # noqa: BLE001
        return {}
    found: dict[int, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid_s, cmd = line.split("|", 1)
        cmd = cmd.strip()
        if any(x.lower() in cmd.lower() for x in CMD_EXCLUDE):
            continue  # 是查询用的 powershell 自己，不是我们的服务
        try:
            found[int(pid_s)] = cmd
        except ValueError:
            continue
    return found


def describe(pid: int) -> str:
    """给 PID 一个人类可读的名字（tasklist 兜底，查不到就只显示 PID）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=15,
        ).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[1].isdigit():
                return parts[0]
    except Exception:  # noqa: BLE001
        pass
    return f"PID {pid}"


def alive(pid: int) -> bool:
    """这个 PID 还活着吗？

    用途：杀之前先问一句。因为 `taskkill /T` 会连带杀掉整棵进程树 ——
    父进程（npm）一死，子进程（vite）也跟着没了，这时候再去杀子进程就会
    收到"找不到进程"，被误报成失败。
    """
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
        return str(pid) in r.stdout
    except Exception:  # noqa: BLE001
        return False


def kill(pid: int) -> bool:
    if not alive(pid):
        return True  # 已经不在了（多半是被别的那次 /T 连坐干掉），算成功
    if os.name == "nt":
        # ⚠️ 同样要 errors=replace：taskkill 的中文提示是 GBK，
        #    按 UTF-8 解码会在**读取线程**里抛异常，把 returncode 冲掉 → 误判成失败。
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        return r.returncode == 0
    r = subprocess.run(["kill", "-TERM", str(pid)], capture_output=True, text=True, errors="replace")
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="一键停掉本地开发环境（Broker + 后端 + 模拟器 + 大屏）")
    ap.add_argument("--dry-run", action="store_true", help="只列出会杀掉的进程，不动手")
    args = ap.parse_args()

    print("=" * 70)
    print("智能家居 AI 助手 · 停止本地服务" + ("（dry-run，只看不动手）" if args.dry_run else ""))
    print("=" * 70)

    targets: dict[int, str] = {}   # pid -> 为什么杀它

    # 1) 按端口
    for port in PORTS:
        for pid in pids_by_port(port):
            targets.setdefault(pid, f"占用端口 {port}")

    # 2) 按命令行关键字
    for pid, cmd in pids_by_command(CMD_KEYWORDS).items():
        short = cmd[:90]
        targets.setdefault(pid, f"命令行匹配（{short}）")

    # 3) 绝不能把自己和启动自己的终端杀掉
    me = {os.getpid(), os.getppid()}
    targets = {p: why for p, why in targets.items() if p not in me}

    if not targets:
        print("  没有找到在跑的服务（环境已经是干净的了）")
        return 0

    print(f"  找到 {len(targets)} 个进程：")
    for pid, why in sorted(targets.items()):
        print(f"    · {pid:<8d} {describe(pid):<24s} {why}")

    if args.dry_run:
        print("\n  dry-run：以上进程不会被杀。去掉 --dry-run 真正执行。")
        return 0

    print()
    ok, bad = 0, []
    for pid in sorted(targets):
        if kill(pid):
            print(f"  ✓ 已停止 {pid}（{describe(pid)}）")
            ok += 1
        else:
            bad.append(pid)

    print()
    still = {p: pids_by_port(p) for p in PORTS}
    remaining = [p for p, v in still.items() if v]
    if remaining:
        print(f"  ⚠️ 这些端口还被占着：{remaining}")
        for p in remaining:
            print(f"     端口 {p} → PID {still[p]}（可能被系统占用或权限不足）")
    else:
        print("  ✓ 端口已全部释放（1883 / 8000 / 5174）")

    if bad:
        print(f"  ⚠️ 这几个没杀掉：{bad}（手动：taskkill /PID <pid> /T /F）")
        return 1
    print(f"\n  共停止 {ok} 个进程。日志文件（broker/api/simulator/web.log）保留，方便回看。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
