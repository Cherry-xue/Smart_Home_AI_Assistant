"""手动触发一场演示场景（安防/照护）。

用法：
    python scripts/trigger_scenario.py gas_leak
    python scripts/trigger_scenario.py smoke --duration-min 6
    python scripts/trigger_scenario.py            # 不带参数 = 列出可选场景

它只往 MQTT 的控制主题发一条消息，由**正在运行的生成器**接住并执行：
    smart_home/{home_id}/system/scenario   {"scenario": "gas_leak", "duration_min": 4}

为什么需要这个入口
------------------
常驻生成器追上真实时间后会降到 1 倍速，而自动场景的间隔是 2.5~5.5 小时。
现场演示不可能等这么久，答辩时也经常需要"我现在就要看一次燃气泄漏"。
所以场景既能自动循环触发，也能被一条命令点亮。

⚠️ 与设备指令的区别
------------------
这条消息走的是**生成器专用**主题，不是设备 `cmd` 主题：
它只让生成器制造"传感器观测到异常"，**不会产生任何设备动作**。
燃气阀关阀、门锁上锁仍然只能由后端生成待确认项、由人确认。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from simulator.registry import load_env  # noqa: E402
from simulator.scenarios import SCENARIOS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="手动触发一场演示场景")
    ap.add_argument("scenario", nargs="?", help="场景名（不带参数则列出全部）")
    ap.add_argument("--duration-min", type=float, default=0.0,
                    help="覆盖场景默认持续时间（分钟）")
    ap.add_argument("--home-id", type=int, default=1)
    args = ap.parse_args()

    if not args.scenario:
        print("可选场景：")
        for name, step in SCENARIOS.items():
            has_ev = "有事件" if step.event else "无事件（只有读数变化）"
            print(f"  {name:<16} {step.label:<22} 持续 {step.duration_min:.0f} 分钟  {has_ev}")
        print("\n用法：python scripts/trigger_scenario.py gas_leak")
        return 0

    if args.scenario not in SCENARIOS:
        print(f"❌ 未知场景 {args.scenario!r}，可选：{', '.join(SCENARIOS)}")
        return 1

    env = load_env()
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("❌ 缺少 paho-mqtt，请用项目环境运行（conda activate ai0707）")
        return 1

    host = env.get("MQTT_HOST", "127.0.0.1")
    port = int(env.get("MQTT_PORT", 1883))
    topic = f"smart_home/{args.home_id}/system/scenario"
    payload: dict[str, object] = {"scenario": args.scenario}
    if args.duration_min:
        payload["duration_min"] = args.duration_min

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id="smart_home_scenario_trigger"
    )
    if env.get("MQTT_USERNAME"):
        client.username_pw_set(env["MQTT_USERNAME"], env.get("MQTT_PASSWORD", ""))

    try:
        client.connect(host, port, 15)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 连不上 broker {host}:{port}（{e}）")
        print("   先启动 broker：gateway/start_mqtt.cmd")
        return 1

    client.loop_start()
    info = client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
    try:
        info.wait_for_publish(timeout=5)
    except (RuntimeError, ValueError):
        pass
    client.loop_stop()
    client.disconnect()

    print(f"✓ 已发送场景触发：{args.scenario} → {topic}")
    print(f"  生成器会从下一个 tick 开始覆盖读数并发一条 event；")
    print(f"  打开告警页（/api/security/alarms）就能看到新告警。")
    print(f"⚠️ 这只制造观测，不产生任何设备动作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
