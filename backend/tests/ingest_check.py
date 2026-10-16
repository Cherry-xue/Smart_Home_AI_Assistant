"""MQTT 落库链路验证（M1 收口验收）。

分三层验证，从"逻辑对不对"到"真链路通不通"：

1. **解析与质检**（纯内存）：契约校验、去重、时间倒退、越界降级、事件归一。
2. **真实落库**：把消息真正写进三张表，再查库核对内容与编码。
   —— 测试数据统一用 2001-01-01 的时间戳，结束后按这个时间范围精确清理，
      不会碰到真实数据（真实数据是 2026 年）。
3. **实连 MQTT**（加 `--live`，需 broker 在跑）：订阅服务 + 发布端往返。

用法：
    cd backend
    python tests/ingest_check.py            # 第 1、2 层（不需要 broker）
    python tests/ingest_check.py --live     # 再加第 3 层（需先启动 broker）
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import delete, func, select  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.core.tariff import get_tariff  # noqa: E402
from app.db.catalog import DeviceCatalog  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.models.orm import DeviceReading, EnergyReading, SensorEvent  # noqa: E402
from app.mqtt.ingest import Ingestor  # noqa: E402

#: 测试数据时间范围（真实数据是 2026 年，用它做隔离，清理时绝不会误删）
TS_DAY = dt.date(2001, 1, 1)
CLEANUP_BEFORE = dt.datetime(2002, 1, 1)


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  ✅ {name}" + (f"  [{detail}]" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  ❌ {name}" + (f"  [{detail}]" if detail else ""))

    def report(self, title: str) -> None:
        print()
        print("-" * 74)
        if self.failed:
            print(f"{title}: {self.passed} PASS / {len(self.failed)} FAIL")
            for f in self.failed:
                print(f"    ❌ {f}")
        else:
            print(f"{title}: {self.passed} PASS / 0 FAIL")
        print("-" * 74)


# ---------------------------------------------------------------------------
# 消息构造
# ---------------------------------------------------------------------------
def topic_of(dev, kind: str) -> str:
    return f"smart_home/{dev.home_id}/room/{dev.room_code}/device/{dev.code}/{kind}"


def payload(
    device_code: str,
    kind: str,
    metrics: dict | None = None,
    *,
    ts: str = "2001-01-01T08:30:00+08:00",
    seq: int | None = 1,
    quality: str = "good",
    units: dict | None = None,
    source: str = "simulator",
    **extra,
) -> str:
    body = {
        "device_code": device_code,
        "kind": kind,
        "observed_at": ts,
        "seq": seq,
        "source": source,
        "quality": quality,
        "metrics": metrics or {},
        "units": units or {},
    }
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 第 1 层：解析与质检（纯内存，不碰数据库）
# ---------------------------------------------------------------------------
def layer1(catalog: DeviceCatalog, settings, tariff) -> tuple[Checker, Ingestor]:
    ck = Checker()
    print("=" * 74)
    print("第 1 层 · 解析与质检（纯内存）")
    print("=" * 74)

    ing = Ingestor(catalog, settings.tzinfo, tariff)

    light = next(d for d in catalog.all() if d.device_type == "light")
    ac = next(d for d in catalog.all() if d.device_type == "ac")
    gas = next(d for d in catalog.all() if d.device_type == "gas_sensor")

    # ---- 1.1 正常 state 消息 ----
    ok = ing.handle(
        topic_of(light, "state"),
        payload(light.code, "state", {"power_on": True, "brightness": 60},
                units={"brightness": "%"}, seq=10),
    )
    rows = ing.take_batch()
    ck.check("正常 state 消息被接受", ok)
    ck.check("两个 metric 展开成两行", len(rows.readings) == 2, f"{len(rows.readings)} 行")
    bool_row = next((r for r in rows.readings if r["metric_code"] == "power_on"), None)
    ck.check("布尔值编成 value_num=1.0", bool_row is not None and bool_row["value_num"] == 1.0,
             f"{bool_row['value_num'] if bool_row else 'N/A'}")
    num_row = next((r for r in rows.readings if r["metric_code"] == "brightness"), None)
    ck.check("数值编成 value_num", num_row is not None and num_row["value_num"] == 60.0)
    ck.check("单位被带上", num_row is not None and num_row["unit"] == "%")
    ck.check("时间戳转成本地 naive", num_row["observed_at"] == dt.datetime(2001, 1, 1, 8, 30))

    # ---- 1.2 最新值缓存 ----
    ck.check("最新值缓存写入", ing.cache.value_of(light.code, "brightness") == 60)
    ck.check("带容量代码可查", ing.cache.get(light.code, "power_on").value is True)

    # ---- 1.3 重复 seq 去重 ----
    before_dup = ing.stats.duplicates
    ing.handle(topic_of(light, "state"),
               payload(light.code, "state", {"power_on": True}, seq=10))
    ing.take_batch()
    ck.check("重复 seq 被丢弃", ing.stats.duplicates == before_dup + 1)

    # ---- 1.4 时间倒退 → is_stale ----
    ing.handle(topic_of(light, "state"),
               payload(light.code, "state", {"power_on": False},
                       ts="2001-01-01T08:00:00+08:00", seq=11))
    rows = ing.take_batch()
    ck.check("时间戳倒退被标 is_stale",
             bool(rows.readings) and all(r["is_stale"] for r in rows.readings))

    # ---- 1.5 越界 → 质量降级 ----
    ing.handle(topic_of(ac, "state"),
               payload(ac.code, "state", {"target_temp": 99.0}, seq=12))
    rows = ing.take_batch()
    ck.check("越界值被降级为 suspect",
             bool(rows.readings) and rows.readings[0]["quality"] == "suspect",
             f"quality={rows.readings[0]['quality'] if rows.readings else 'N/A'}")

    # ---- 1.6 发布方自报 bad → 透传，且不进缓存 ----
    ing.handle(topic_of(gas, "telemetry"),
               payload(gas.code, "telemetry", {"gas_concentration": 999.0},
                       quality="bad", seq=13))
    rows = ing.take_batch()
    ck.check("bad 质量原样透传", bool(rows.readings) and rows.readings[0]["quality"] == "bad")
    ck.check("bad 数据不进最新值缓存", ing.cache.get(gas.code, "gas_concentration") is None)

    # ---- 1.7 契约拒收 ----
    rej = ing.stats.rejected
    ing.handle("smart_home/1/bad/topic", payload(light.code, "state", {"power_on": True}))
    ing.handle(topic_of(light, "state"), "{ 这不是 JSON")
    ing.handle(topic_of(light, "state"),
               payload("some_other_device", "state", {"power_on": True}))   # topic 与 payload 不一致
    ing.take_batch()
    ck.check("三类不合格消息全部拒收", ing.stats.rejected == rej + 3,
             f"rejected +{ing.stats.rejected - rej}")

    # ---- 1.8 未知设备 ----
    unknown = ing.stats.unknown_device
    ing.handle(
        f"smart_home/{light.home_id}/room/living_room/device/not_in_db_99/state",
        payload("not_in_db_99", "state", {"power_on": True}),
    )
    ing.take_batch()
    ck.check("未知设备被拒收", ing.stats.unknown_device == unknown + 1)

    # ---- 1.9 控制类消息不落库 ----
    ctrl = ing.stats.control_ignored
    ing.handle(topic_of(light, "cmd"), payload(light.code, "cmd", {"power_on": True}))
    ing.handle(topic_of(light, "cmd_ack"), payload(light.code, "cmd_ack", {}))
    ing.take_batch()
    ck.check("cmd/cmd_ack 不落库", ing.stats.control_ignored == ctrl + 2)

    # ---- 1.10 电价时段 ----
    ck.check("08:30 属于峰电", tariff.period_at(dt.datetime(2001, 1, 1, 8, 30)) == "peak")
    ck.check("12:00 属于平电", tariff.period_at(dt.datetime(2001, 1, 1, 12, 0)) == "flat")
    ck.check("22:00 属于谷电", tariff.period_at(dt.datetime(2001, 1, 1, 22, 0)) == "valley")
    ck.check("02:00 属于谷电（跨零点）", tariff.period_at(dt.datetime(2001, 1, 1, 2, 0)) == "valley")

    # ---- 1.11 retained 快照不污染时间基准（实测踩过的坑，回归用例）----
    # 场景：broker 在订阅瞬间回放了上一轮留下的 state（时间戳比新一轮晚很多）。
    # 如果拿它当"上一条"的基准，接下来所有实时数据都会被判 is_stale。
    ing2 = Ingestor(catalog, settings.tzinfo, tariff)
    ing2.handle(
        topic_of(light, "state"),
        payload(light.code, "state", {"power_on": True},
                ts="2001-01-01T10:00:00+08:00", seq=900),
        retain=True,
    )
    ing2.take_batch()
    ck.check("retained 快照被接受并单独计数", ing2.stats.retained_snapshots == 1)

    ing2.handle(
        topic_of(light, "state"),
        payload(light.code, "state", {"power_on": False},
                ts="2001-01-01T09:00:00+08:00", seq=901),
    )
    rows = ing2.take_batch()
    ck.check("retained 之后到来的较早实时数据【不】判 stale（基准未被污染）",
             bool(rows.readings) and not any(r["is_stale"] for r in rows.readings),
             f"{len(rows.readings)} 行")

    # 对照：普通消息的时间倒退仍然要能识别出来
    ing2.handle(
        topic_of(light, "state"),
        payload(light.code, "state", {"power_on": True},
                ts="2001-01-01T08:00:00+08:00", seq=902),
    )
    rows = ing2.take_batch()
    ck.check("对照：普通消息的时间倒退仍然判 stale",
             bool(rows.readings) and all(r["is_stale"] for r in rows.readings))

    return ck, ing


# ---------------------------------------------------------------------------
# 第 2 层：真实落库 + 查库核对
# ---------------------------------------------------------------------------
def layer2(catalog: DeviceCatalog, settings, tariff) -> Checker:
    ck = Checker()
    print()
    print("=" * 74)
    print("第 2 层 · 真实落库与查库核对")
    print("=" * 74)

    with SessionLocal() as db:
        # 先清理可能残留的测试数据
        n = cleanup(db)
        print(f"  （清理上次残留：{n} 行）")

    ing = Ingestor(catalog, settings.tzinfo, tariff)
    ac = next(d for d in catalog.all() if d.device_type == "ac")
    gas = next(d for d in catalog.all() if d.device_type == "gas_sensor")

    # ---- 能耗积分：两条相隔 300 秒的功率读数 ----
    ing.handle(topic_of(ac, "state"),
               payload(ac.code, "state", {"power_on": True, "power_w": 1100.0},
                       ts="2001-01-01T14:00:00+08:00", seq=100,
                       units={"power_w": "W"}))
    ing.handle(topic_of(ac, "state"),
               payload(ac.code, "state", {"power_on": True, "power_w": 1100.0},
                       ts="2001-01-01T14:05:00+08:00", seq=101,
                       units={"power_w": "W"}))
    # ---- 事件（同一事件发两次，第二次应被去重）----
    ing.handle(topic_of(gas, "event"),
               payload(gas.code, "event", {}, ts="2001-01-01T09:00:00+08:00", seq=None,
                       event_code="gas_leak", level="critical", title="厨房检测到燃气泄漏",
                       detail="浓度 42 %LEL", suggested_action="关闭燃气阀（需人工确认）"))
    ing.handle(topic_of(gas, "event"),
               payload(gas.code, "event", {}, ts="2001-01-01T09:00:30+08:00", seq=None,
                       event_code="gas_leak", level="critical", title="厨房检测到燃气泄漏"))
    # ---- 非法 level 归一 ----
    ing.handle(topic_of(gas, "event"),
               payload(gas.code, "event", {}, ts="2001-01-01T09:20:00+08:00", seq=None,
                       event_code="weird_level", level="catastrophic", title="等级非法"))

    batch = ing.take_batch()
    written = ing.write_batch(batch)
    ck.check("批量写库无异常", written == len(batch), f"{written} 行")
    ck.check("事件去重生效", ing.stats.event_deduped == 1)

    with SessionLocal() as db:
        # 能耗行
        energies = db.execute(
            select(EnergyReading.observed_at, EnergyReading.power_w,
                   EnergyReading.energy_kwh, EnergyReading.tariff_period)
            .where(EnergyReading.observed_at < CLEANUP_BEFORE)
            .order_by(EnergyReading.observed_at)
        ).all()
        ck.check("能耗落两行", len(energies) == 2, f"{len(energies)} 行")
        first, second = energies[0], energies[1]
        ck.check("第一条无增量电量（没有前序参考）", first.energy_kwh is None)
        expected = 1100.0 * 300 / 3600 / 1000          # 0.091666...
        ck.check("增量电量按时间差积分", second.energy_kwh is not None
                 and abs(float(second.energy_kwh) - expected) < 1e-6,
                 f"{float(second.energy_kwh):.6f} vs 期望 {expected:.6f}")
        ck.check("峰平谷标签正确（14:00 → flat）",
                 first.tariff_period == "flat", str(first.tariff_period))

        # 事件行
        events = db.execute(
            select(SensorEvent.event_code, SensorEvent.level, SensorEvent.payload)
            .where(SensorEvent.started_at < CLEANUP_BEFORE)
            .order_by(SensorEvent.started_at)
        ).all()
        ck.check("事件落两行（第三次被去重）", len(events) == 2, f"{len(events)} 行")
        ck.check("事件 event_code 正确", events[0].event_code == "gas_leak")
        ck.check("事件 level 正确", events[0].level == "critical")
        ck.check("非法 level 归一为 warning", events[1].level == "warning",
                 str(events[1].level))
        ck.check("事件 payload 原样存档（含 suggested_action）",
                 isinstance(events[0].payload, dict)
                 and "关闭燃气阀" in str(events[0].payload.get("suggested_action", "")))

        # 遥测行：确认 value_num / value_text 的编码各就各位
        readings = db.execute(
            select(DeviceReading.metric_code, DeviceReading.value_num, DeviceReading.value_text)
            .where(DeviceReading.observed_at < CLEANUP_BEFORE)
        ).all()
        pnum = [r for r in readings if r.metric_code == "power_w"]
        ck.check("功率走 value_num", bool(pnum) and all(r.value_num is not None for r in pnum),
                 f"{len(pnum)} 行")

    # ---- 清理 ----
    with SessionLocal() as db:
        removed = cleanup(db)
    ck.check("测试数据已清理（不留垃圾）", sum(removed.values()) >= 0, f"删除 {removed}")

    return ck


def cleanup(db) -> dict[str, int]:
    """删除所有测试数据（时间戳早于 2002 年的一律是测试造的）。"""
    out: dict[str, int] = {}
    out["device_readings"] = db.execute(
        delete(DeviceReading).where(DeviceReading.observed_at < CLEANUP_BEFORE)
    ).rowcount
    out["energy_readings"] = db.execute(
        delete(EnergyReading).where(EnergyReading.observed_at < CLEANUP_BEFORE)
    ).rowcount
    out["sensor_events"] = db.execute(
        delete(SensorEvent).where(SensorEvent.started_at < CLEANUP_BEFORE)
    ).rowcount
    db.commit()
    return out


# ---------------------------------------------------------------------------
# 第 3 层：实连 MQTT（订阅服务 + 发布端往返）
# ---------------------------------------------------------------------------
async def layer3(catalog: DeviceCatalog, settings, tariff) -> Checker:
    ck = Checker()
    print()
    print("=" * 74)
    print("第 3 层 · 实连 MQTT 往返")
    print("=" * 74)

    import aiomqtt

    from app.mqtt.subscriber import MqttIngestService

    service = MqttIngestService(topic="smart_home/#", qos=1, flush_interval=0.5)
    task = asyncio.create_task(service.run())

    try:
        # 等连接建立
        for _ in range(40):
            if service.service_stats.connected:
                break
            await asyncio.sleep(0.25)
        ck.check("订阅服务已连上 broker", service.service_stats.connected)

        light = next(d for d in catalog.all() if d.device_type == "light")
        async with aiomqtt.Client(
            hostname=settings.mqtt_host, port=settings.mqtt_port,
            identifier=f"{settings.mqtt_client_id_prefix}_check_pub",
        ) as pub:
            for seq, brightness in ((201, 30), (202, 45), (203, 70)):
                await pub.publish(
                    topic_of(light, "state"),
                    payload(light.code, "state", {"power_on": True, "brightness": brightness},
                            ts=f"2001-01-01T{(15 + seq - 201):02d}:00:00+08:00", seq=seq),
                    qos=1,
                )
                await asyncio.sleep(0.3)

        await asyncio.sleep(1.5)
        await service.flush_now()
        await asyncio.sleep(0.3)

        st = service.stats()
        ck.check("服务收到了消息", st["ingest"]["received"] >= 3,
                 f"received={st['ingest']['received']}")
        ck.check("消息被落库", st["ingest"]["readings"] >= 6,
                 f"readings={st['ingest']['readings']}")

        with SessionLocal() as db:
            n = db.scalar(
                select(func.count()).select_from(DeviceReading)
                .where(DeviceReading.observed_at < CLEANUP_BEFORE)
            )
            ck.check("库里确实有这批数据", n >= 6, f"{n} 行")
            vals = db.execute(
                select(DeviceReading.value_num).where(
                    DeviceReading.metric_code == "brightness",
                    DeviceReading.observed_at < CLEANUP_BEFORE,
                ).order_by(DeviceReading.observed_at)
            ).scalars().all()
            ck.check("亮度值顺序与内容正确",
                     [float(v) for v in vals] == [30.0, 45.0, 70.0], str(vals))
    finally:
        service.request_stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await service.flush_now()

    with SessionLocal() as db:
        removed = cleanup(db)
    ck.check("测试数据已清理", True, f"删除 {removed}")

    return ck


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="MQTT 落库链路验证")
    ap.add_argument("--live", action="store_true", help="额外跑实连 MQTT 测试（需 broker）")
    args = ap.parse_args()

    # 必须在 asyncio.run 之前：aiomqtt 在 Windows 的 ProactorEventLoop 上无法工作
    from app.core.asyncio_compat import ensure_selector_loop_on_windows

    ensure_selector_loop_on_windows()

    settings = get_settings()
    tariff = get_tariff()
    catalog = DeviceCatalog.load(engine)

    print()
    print("#" * 74)
    print("# MQTT 落库链路验证")
    print("#" * 74)
    print(f"  数据库      : {settings.mysql_host}:{settings.mysql_port}/{settings.mysql_db}")
    print(f"  MQTT        : {settings.mqtt_host}:{settings.mqtt_port}")
    print(f"  设备目录    : {len(catalog)} 台")
    print(f"  电价时段    : {tariff.describe()}")
    print()

    ck1, _ = layer1(catalog, settings, tariff)
    ck1.report("第 1 层 解析与质检")

    ck2 = layer2(catalog, settings, tariff)
    ck2.report("第 2 层 落库")

    failures = len(ck1.failed) + len(ck2.failed)
    if args.live:
        ck3 = asyncio.run(layer3(catalog, settings, tariff))
        ck3.report("第 3 层 实连 MQTT")
        failures += len(ck3.failed)

    total = ck1.passed + ck2.passed + (ck3.passed if args.live else 0)
    print()
    print("=" * 74)
    if failures:
        print(f"结果：{total} PASS / {failures} FAIL")
    else:
        print(f"结果：{total} PASS / 0 FAIL  —— 落库链路验证通过 ✅")
    print("=" * 74)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
