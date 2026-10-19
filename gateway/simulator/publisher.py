"""数据生成器主程序：按时间推演全屋状态并发布到 MQTT。

用法
----
    # ⭐ 推荐：补齐"过去 24 小时"的历史数据，终点正好落在当前时刻
    #   （back:1440 = 从当前往前 1440 模拟分钟开始；duration 与它相等就不会跑到未来）
    python -m gateway.simulator.publisher --start back:1440 --duration 1440 --speed 600

    # 快速验证：模拟 2 小时、加速 300 倍、只打印不发布
    python -m gateway.simulator.publisher --speed 300 --duration 120 --dry-run

    # 实时跑（会随时间倍速冲到未来，仅演示用）
    python -m gateway.simulator.publisher --speed 60 --duration 1440

⚠️ 为什么要用 `--start back:N`
-----------------------------
倍速跑满 24 小时后，模拟时间会冲到 `当前时刻 + 24 小时`，界面上到处是
"未来时间"，数据质量判定也会把它们标成异常。而 `auto`（默认）的续跑规则又是
"库里最新 ≥ 当前时刻就从它之后接着跑"，于是越跑越超前，再也回不来。

`back:N` 让起点回到"当前时刻往前 N 分钟"，配合相等的 `--duration N`，
这一轮的终点正好是**现在**。启动日志会打印"模拟终点"并标注是否晚于当前时刻。

常用参数
--------
    --speed N      时间倍速（1 = 真实时间；60 = 1 秒跑 1 分钟）
    --tick N       每个 tick 代表多少【模拟秒】（默认 5）
    --duration N   总共模拟多少【模拟分钟】，0 表示一直跑
    --start X      auto（默认，从库里最新续跑）/ now / back:<分钟> / ISO 时间
    --fault-rate F 坏数据注入比例（默认 0.05）
    --scenario X   none / random / gas_leak / water_leak / smoke /
                   no_activity / night_wake / intrusion
    --dry-run      只打印不发布（不必启动 broker 也能看数据长什么样）

⚠️ 本进程【只发布观测数据】，永远不产生控制指令。
   门锁/燃气阀/总闸的状态只是"上报当前状态"，不是"接受控制"。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import queue
import random
import re
import sys
import time
from pathlib import Path

# 支持两种运行方式：python -m gateway.simulator.publisher  或  python publisher.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from .faults import apply as apply_faults
    from .physics import ACTUATOR_TYPES, HomeSimulator
    from .registry import (
        latest_observed_at,
        load_devices,
        load_env,
        load_last_commands,
        load_profiles,
    )
    from .scenarios import ScenarioRunner, apply_overrides, build_event_message
except ImportError:  # 直接当脚本运行
    from faults import apply as apply_faults
    from physics import ACTUATOR_TYPES, HomeSimulator
    from registry import (
        latest_observed_at,
        load_devices,
        load_env,
        load_last_commands,
        load_profiles,
    )
    from scenarios import ScenarioRunner, apply_overrides, build_event_message

QOS = {"state": 1, "telemetry": 0, "event": 1}
RETAIN = {"state": True, "telemetry": False, "event": False}


def build_topic(home_id: int, room_code: str, device_code: str, kind: str) -> str:
    return f"smart_home/{home_id}/room/{room_code}/device/{device_code}/{kind}"


def build_payload(emission, observed_at: dt.datetime, home_id: int) -> str:
    s = emission.sample
    observed = observed_at + dt.timedelta(seconds=emission.observed_offset_sec)
    body = {
        "device_code": s.device.code,
        "room_code": s.device.room_code,
        "kind": s.kind,
        "observed_at": observed.isoformat(timespec="milliseconds"),
        "seq": emission.seq,
        "source": "simulator",
        "quality": emission.quality,
        "metrics": s.metrics,
        "units": s.units,
    }
    if emission.fault:
        body["fault_injected"] = emission.fault      # 便于排障：标出这条是故意造的
    return json.dumps(body, ensure_ascii=False)


def build_event_payload(dev, ev: dict, observed_at: dt.datetime) -> str:
    body = {
        "device_code": dev.code,
        "room_code": dev.room_code,
        "kind": "event",
        "observed_at": observed_at.isoformat(timespec="milliseconds"),
        "seq": None,
        "source": "simulator",
        "quality": "good",
        "metrics": {},
        "units": {},
        **ev,
    }
    return json.dumps(body, ensure_ascii=False)


def resolve_start(args: argparse.Namespace, env: dict[str, str]) -> tuple[dt.datetime, str]:
    """决定模拟起始时间，返回 (起始时刻, 说明)。

    `auto`（默认）的规则是 **max(库里最新数据, 当前时刻)**：
    - 库里没有数据 → 用当前时刻（第一次跑，数据时间贴近真实）；
    - 库里最新数据已经晚于当前时刻（模拟器曾按倍速跑到"未来"）→ 从它之后续跑。

    为什么需要这个判断：如果每轮都从"当前时刻"硬开始，而库里 / broker 上
    retained 的 state 已有**更晚时间戳**的数据，数据源的时间轴就倒退了。
    订阅端有一层"时间倒退 → is_stale"的质量判定，会把整轮新数据都判成
    "补发数据"（实测踩到：一轮 510 条被误标，缓存也跟着失效）。
    """
    now = dt.datetime.now().astimezone().replace(microsecond=0)

    if args.start == "now":
        return now, "（--start now）"

    # `back:<分钟>` —— 从"当前时刻往前 N 分钟"开始补历史。
    # ⚠️ 为什么需要它：按倍速跑满 24 小时后，模拟时间会冲到 now+24h，
    # 界面上到处是"未来时间"，数据质量判定也会把它们全标成异常。
    # 用 `--start back:1440 --duration 1440` 可以让这一轮的终点**正好落在当前时刻**。
    if args.start.startswith("back:"):
        minutes = float(args.start.split(":", 1)[1])
        return (
            (now - dt.timedelta(minutes=minutes)).replace(microsecond=0),
            f"（回溯 {minutes:.0f} 分钟补历史）",
        )

    if args.start != "auto":
        return dt.datetime.fromisoformat(args.start), f"（--start {args.start}）"

    latest = latest_observed_at(env)
    if latest is None:
        return now, "（库里无历史数据，取当前时刻）"

    latest_aware = latest.astimezone()
    if latest_aware >= now:
        nxt = (latest_aware + dt.timedelta(seconds=args.tick)).replace(microsecond=0)
        note = f"（续跑：库里已有至 {latest_aware:%m-%d %H:%M:%S} 的数据，避免时间轴回退）"
        # ⚠️ 防呆：库里已经跑到未来了还继续续跑，只会越跑越超前，再也回不来。
        # 这时正确做法是 `--start back:N` 把时间轴拉回现在。
        if latest_aware > now + dt.timedelta(minutes=5):
            ahead = (latest_aware - now).total_seconds() / 3600
            note += (
                f" ⚠️ 库里最新数据已比当前时刻超前 {ahead:.1f} 小时，"
                f"继续跑会更超前；建议改用 `--start back:<分钟>` 把时间轴拉回现在"
            )
        return nxt, note
    return now, f"（库里最新 {latest_aware:%H:%M:%S} 早于当前时刻，取当前时刻）"


# ---------------------------------------------------------------------------
# 控制指令：后端下发 → 模拟器应用
#
# 没有这段，"执行场景"就是个空壳：后端写了指令记录、发了 MQTT，
# 但设备状态纹丝不动 —— 演示时看起来就像"点了没反应"。
#
# 有这段之后闭环才完整：
#   后端下发 cmd → 模拟器应用覆盖 → 后续 state 上报反映新状态
#   → 订阅端落库/进缓存 → API 查询立刻能看到变化
# ---------------------------------------------------------------------------
CMD_TOPIC_RE = re.compile(
    r"^smart_home/(?P<home_id>\d+)/room/(?P<room_code>[A-Za-z0-9_\-]+)"
    r"/device/(?P<device_code>[A-Za-z0-9_\-]+)/cmd$"
)


def handle_command(sim, client, msg) -> None:  # noqa: ANN001
    """处理一条控制指令（跑在 paho 的回调线程里）。

    做三件事：**解析 → 校验并应用 → 回 ack**。

    ⚠️ 校验不通过**也要回 ack**：后端必须知道"设备拒绝了这条指令"，
    否则它会一直以为自己发成功了 —— 那是比"下发失败"更糟的状态
    （记录显示成功、设备却没动）。
    """
    topic = str(msg.topic)
    m = CMD_TOPIC_RE.match(topic)
    if not m:
        return

    device_code = m.group("device_code")
    ack_topic = topic[: -len("/cmd")] + "/cmd_ack"

    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 指令报文解析失败（{topic}）：{type(e).__name__}: {e}")
        return

    metrics = data.get("metrics") or {}
    now = sim.last_now or dt.datetime.now()
    results: list[dict] = []
    all_ok = True

    for capability, value in metrics.items():
        ok, note = sim.apply_command(device_code, capability, value, now)
        all_ok = all_ok and ok
        results.append({"capability": capability, "value": value, "accepted": ok, "note": note})
        print(f"  {'✅' if ok else '❌'} 收到指令 {device_code}.{capability}={value!r} → {note}")

    try:
        client.publish(
            ack_topic,
            json.dumps(
                {
                    "device_code": device_code,
                    "kind": "cmd_ack",
                    "command_id": data.get("command_id"),
                    "accepted": all_ok,
                    "results": results,
                    "observed_at": now.isoformat(timespec="milliseconds"),
                    "source": "simulator",
                },
                ensure_ascii=False,
            ),
            qos=1,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 回执发送失败：{type(e).__name__}: {e}")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="智能家居虚拟设备数据生成器（只产观测数据，不产生控制指令）"
    )
    ap.add_argument("--speed", type=float, default=1.0, help="时间倍速，1=真实时间")
    ap.add_argument("--tick", type=float, default=5.0, help="每 tick 的模拟秒数")
    ap.add_argument("--duration", type=float, default=0.0, help="模拟总分钟数，0=一直跑")
    ap.add_argument("--fault-rate", type=float, default=0.05, help="坏数据注入比例 0~1")
    ap.add_argument("--scenario", default="none", help="预置场景（见文件头）")
    ap.add_argument(
        "--scenario-repeat", action="store_true",
        help="循环触发场景（约每 2.5~5.5 模拟小时一场，随机不重复）。"
             "常驻生成器要它才有持续的异常事件，否则跑过某个时间点后就再也不会有事件",
    )
    ap.add_argument("--start", default="auto",
                    help="模拟起始时间：auto（默认，从库里最新数据续跑）/ now / ISO 时间")
    ap.add_argument("--seed", type=int, default=20261015, help="随机种子（可复现）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发布")
    ap.add_argument("--preview", type=int, default=6, help="dry-run 时打印多少条样例")
    ap.add_argument("--progress-every", type=float, default=30.0, help="每多少模拟分钟打一次进度")
    ap.add_argument(
        "--max-ahead", type=float, default=5.0,
        help="常驻模式下模拟时间最多领先真实时间多少【分钟】（0=不限）。"
             "倍速演化到接近\"现在\"后会自动减速跟随真实时间，不会冲到未来",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    env = load_env()
    profiles = load_profiles()
    devices = load_devices(env)
    if not devices:
        print("❌ 数据库里没有启用的设备，请先执行 mysql/init/04_seed.sql")
        return 1

    # ⚠️ 多套房：每台设备按**自己的** home_id 发 topic。
    # 早期版本用 devices[0].home_id 一刀切，一旦库里有第二套房，
    # 它的设备数据会全部发到第一套房的 topic 下 —— 那种"数据看着有、但挂错房子"
    # 的错最难查（后端不会报错，只是把 A 家的温度记到 B 家）。
    home_ids = sorted({d.home_id for d in devices})
    rng = random.Random(args.seed)
    sim = HomeSimulator(devices, profiles, seed=args.seed)
    runner = ScenarioRunner(args.scenario, rng, auto_repeat=args.scenario_repeat)

    # ---- 重启后恢复设备状态 ----
    # 模拟器状态只在内存里，不恢复的话"重启一次 = 之前开的灯全灭、空调温度跳回默认"。
    # 只重放 status='executed' 的指令；高风险的 pending_confirm 永远不会被重放。
    try:
        restored = load_last_commands(env)
    except Exception as e:  # noqa: BLE001
        restored = {}
        print(f"  ⚠️ 读取历史指令失败（{type(e).__name__}: {e}），按默认状态启动")
    if restored:
        base_now = dt.datetime.now().astimezone()
        applied = sum(
            1 for (code, cap), val in restored.items()
            if sim.apply_command(code, cap, val, base_now)[0]
        )
        print(f"  已恢复历史指令状态 : {applied}/{len(restored)} 条（重启不丢开关状态）")

    start, start_note = resolve_start(args, env)

    tick_min = args.tick / 60.0
    sleep_sec = tick_min * 60.0 / max(args.speed, 1e-9)
    total_ticks = int(args.duration / tick_min) if args.duration > 0 else 0

    print("=" * 78)
    print("智能家居虚拟设备数据生成器")
    print("=" * 78)
    print(f"  家庭        : {', '.join(map(str, home_ids))}（共 {len(home_ids)} 套，设备各按自己的 home 发布）")
    print(f"  设备数      : {len(devices)}（其中高风险 {sum(1 for d in devices if d.high_risk)} 台，只上报状态）")
    print(f"  模拟起点    : {start.isoformat()} {start_note}")
    print(f"  时间倍速    : {args.speed}x   （每 tick {args.tick:.0f} 模拟秒 = {sleep_sec:.3f} 真实秒）")
    print(f"  模拟时长    : {args.duration:.0f} 分钟" if args.duration else "  模拟时长    : 不限")
    if args.duration > 0:
        end = start + dt.timedelta(minutes=args.duration)
        flag = "  ⚠️ 晚于当前时刻，界面会出现「未来时间」数据" if end > dt.datetime.now().astimezone() else "  ✅ 不晚于当前时刻"
        print(f"  模拟终点    : {end.isoformat(timespec='seconds')}{flag}")
    print(f"  坏数据比例  : {args.fault_rate:.0%}")
    print(f"  场景        : {runner.steps_summary}")
    print(f"  发布目标    : {'[DRY-RUN 只打印]' if args.dry_run else env.get('MQTT_HOST') + ':' + env.get('MQTT_PORT', '1883')}")
    print("=" * 78)
    print()

    client = None
    topic_count: dict[str, int] = {}
    total_emitted = 0

    #: 手动触发场景的请求队列（MQTT 回调线程写入，主循环消费）
    #: 用队列而不是共享变量：paho 的回调在网络线程里跑，主循环在另一条线程上读，
    #: 用 list 裸着传会有竞态（读的同时追加，行为不确定）。
    scenario_requests: queue.Queue = queue.Queue()

    if not args.dry_run:
        import paho.mqtt.client as mqtt

        # 订阅全部套房的指令与场景主题（`+` 通配 home_id）
        cmd_topic = "smart_home/+/room/+/device/+/cmd"
        scenario_topic = "smart_home/+/system/scenario"

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{env.get('MQTT_CLIENT_ID_PREFIX', 'smart_home_sim')}_gen",
        )
        if env.get("MQTT_USERNAME"):
            client.username_pw_set(env["MQTT_USERNAME"], env.get("MQTT_PASSWORD", ""))

        def _on_connect(c, userdata, flags, rc, properties=None):  # noqa: ANN001
            # 连上（含断线重连）后都要重新订阅
            c.subscribe(cmd_topic, qos=1)
            c.subscribe(scenario_topic, qos=1)
            print(f"  已订阅控制指令   : {cmd_topic}")
            print(f"  已订阅场景触发   : {scenario_topic}")

        def _on_message(c, userdata, message):  # noqa: ANN001
            # ⚠️ 订阅用了通配 `smart_home/+/system/scenario`，收到的 topic 是具体的
            #    （比如 smart_home/1/system/scenario），所以这里必须用后缀判断，
            #    不能用 == 比较订阅串。
            if message.topic.endswith("/system/scenario"):
                # 手动触发场景：只入队，真正生效在主循环里（避免在回调线程改模拟器状态）
                try:
                    req = json.loads(message.payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    print(f"  ⚠️ 场景触发报文解析失败：{e}", flush=True)
                    return
                scenario_requests.put(req if isinstance(req, dict) else {})
                return
            handle_command(sim, c, message)

        client.on_connect = _on_connect
        client.on_message = _on_message

        try:
            client.connect(env.get("MQTT_HOST", "127.0.0.1"), int(env.get("MQTT_PORT", 1883)), 30)
        except Exception as e:  # noqa: BLE001
            print(f"❌ 连不上 MQTT Broker（{env.get('MQTT_HOST')}:{env.get('MQTT_PORT')}）：{e}")
            print("   请先启动 broker：双击 gateway/start_mqtt.cmd")
            print("   或者加 --dry-run 先看数据长什么样")
            return 1
        client.loop_start()

    previewed = 0
    t0 = time.perf_counter()
    next_progress = args.progress_every

    #: 运行中重扫设备表的间隔（秒）。
    #: ⚠️ 生成器是**常驻进程**，启动时只读一次设备表。用户在界面上「添加设备」之后，
    #:    不重扫就永远不会为它产数据 —— 表现是"界面上在线，但一直没有读数"，
    #:    很容易被误判成 bug。30 秒是"够快"和"别太频繁查库"之间的折中。
    RESCAN_SEC = 30.0
    next_rescan = time.perf_counter() + RESCAN_SEC
    rescan_done = 0

    try:
        n = 0
        while True:
            now = start + dt.timedelta(minutes=n * tick_min)
            elapsed_min = n * tick_min

            # ---- 周期性重扫设备表（支持"运行中在界面上增删设备"）----
            if not args.dry_run and time.perf_counter() >= next_rescan:
                next_rescan = time.perf_counter() + RESCAN_SEC
                try:
                    fresh = load_devices(env)
                except Exception as e:  # noqa: BLE001
                    # 查库失败不能让生成器停摆：沿用当前清单继续跑
                    print(f"  ⚠️ 重扫设备表失败（{type(e).__name__}: {e}），沿用当前清单", flush=True)
                else:
                    added, updated, removed = sim.sync_from(fresh)
                    rescan_done += 1
                    if added or updated or removed:
                        # ⚠️ flush=True 是必须的：常驻进程的 stdout 重定向到文件时是
                        # **块缓冲**，不加 flush 这条日志要攒够 8KB 才落盘 ——
                        # 实测踩过：simulator.log 一直是 0 字节，等于没有日志。
                        print(
                            f"  🔄 设备清单已同步："
                            f"+{len(added)} 新增 {added}；"
                            f"~{len(updated)} 更新 {updated}；"
                            f"-{len(removed)} 停用 {removed}",
                            flush=True,
                        )
                        devices = fresh
                    elif rescan_done == 1:
                        # 第一次重扫无论有没有变化都报一次 ——
                        # 否则"重扫到底有没有在跑"只能靠猜（这次就是靠这条才定位到问题）
                        print(
                            f"  ✓ 设备表重扫正常（{len(fresh)} 台，本次无变化），此后每 "
                            f"{RESCAN_SEC:.0f} 秒对一次账",
                            flush=True,
                        )
                    # 存量设备的在线时长/tick 统计不受影响，只换清单

            samples = sim.step(now, tick_min)

            # 处理手动触发请求（演示/联调用）：从"当前模拟时刻"开始跑一场
            while True:
                try:
                    req = scenario_requests.get_nowait()
                except queue.Empty:
                    break
                name = str(req.get("scenario") or "")
                dur = req.get("duration_min")
                if runner.trigger(name, elapsed_min, float(dur) if dur else None):
                    print(
                        f"  🎬 手动触发场景 {name}（第 {elapsed_min:.0f} 模拟分钟起，"
                        f"{float(dur) if dur else '默认'}分钟）",
                        flush=True,
                    )
                else:
                    print(f"  ⚠️ 未知场景 {name!r}，可选项见 gateway/simulator/scenarios.py", flush=True)

            ov = runner.active(elapsed_min)
            samples = apply_overrides(samples, ov)

            emissions, stats = apply_faults(samples, rng, args.fault_rate, seq=sim.seq)
            total_emitted += stats.emitted

            for em in emissions:
                s = em.sample
                # 用**这台设备自己的** home_id（多套房时不能统一用第一家）
                topic = build_topic(s.device.home_id, s.device.room_code, s.device.code, s.kind)
                payload = build_payload(em, now, s.device.home_id)
                topic_count[topic] = topic_count.get(topic, 0) + 1
                if args.dry_run:
                    if previewed < args.preview:
                        previewed += 1
                        print(f"[{now:%H:%M:%S}] {topic}")
                        print(f"        {payload}")
                else:
                    assert client is not None
                    client.publish(topic, payload, qos=QOS[s.kind], retain=RETAIN[s.kind])

            # 场景事件（只在进入窗口时发一次）
            ev_pair = build_event_message(ov, devices) if ov else None
            if ev_pair:
                dev, ev = ev_pair
                topic = build_topic(dev.home_id, dev.room_code, dev.code, "event")
                payload = build_event_payload(dev, ev, now)
                topic_count[topic] = topic_count.get(topic, 0) + 1
                if args.dry_run:
                    print(f"\n🔴 [事件] {topic}")
                    print(f"        {payload}\n")
                else:
                    assert client is not None
                    client.publish(topic, payload, qos=1, retain=False)

            n += 1
            if args.duration > 0 and elapsed_min >= next_progress:
                pct = elapsed_min / args.duration * 100
                print(f"  进度 {elapsed_min:6.0f}/{args.duration:.0f} 模拟分钟 ({pct:5.1f}%) | "
                      f"累计发布 {total_emitted} 条 | {stats.summary()}")
                next_progress += args.progress_every

            if total_ticks and n >= total_ticks:
                break

            if sleep_sec > 0 and not args.dry_run:
                time.sleep(sleep_sec)

            # ⚠️ 超前保护：倍速下模拟时间会一路冲到未来（界面全是未来时间、
            # 数据质量判定也会跟着乱）。超过 --max-ahead 就暂停推进，
            # 等真实时间追上来 —— 每次只睡最多 1 秒，保证 MQTT 心跳与
            # 指令回调不受影响（开灯指令仍然秒回）。
            #
            # 补历史时（--start back:N）模拟时间落后于真实时间，drift 为负，
            # 不会触发等待，照常全速跑；跑到"接近现在"才自动减速跟随。
            if args.max_ahead > 0 and not args.dry_run:
                limit_sec = args.max_ahead * 60.0
                drift = (now - dt.datetime.now().astimezone()).total_seconds()
                while drift > limit_sec:
                    time.sleep(min(1.0, drift - limit_sec))
                    drift = (now - dt.datetime.now().astimezone()).total_seconds()
    except KeyboardInterrupt:
        print("\n  收到中断，收尾中…")
    finally:
        if client is not None:
            time.sleep(0.3)
            client.loop_stop()
            client.disconnect()

    wall = time.perf_counter() - t0
    print()
    print("=" * 78)
    print("生成结束")
    print("=" * 78)
    print(f"  模拟时长    : {(n * tick_min):.0f} 模拟分钟 = {(n * tick_min) / 60:.2f} 小时")
    print(f"  实际耗时    : {wall:.1f} 秒（加速比 {((n * tick_min) * 60 / wall if wall else 0):.0f}x）")
    print(f"  累计发布    : {total_emitted} 条")
    print(f"  涉及 topic  : {len(topic_count)} 个")
    if args.dry_run:
        print("\n  ⚠️ 本次是 dry-run，没有真的发到 MQTT。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
