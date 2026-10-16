"""MQTT 消息落库。

数据去向
--------
| 消息 kind | 落到哪 |
|---|---|
| `state` / `telemetry` | `device_readings`（EAV：一个 metric 一行） |
| 含 `power_w` 的消息 | 额外落 `energy_readings`（功率 + 增量电量 + 峰平谷标签） |
| `event` | `sensor_events`（一次事件一行，原样存档 payload） |
| `cmd` / `cmd_ack` | **不落库**（那是"后端 → 设备"方向，不属于观测数据） |

为什么 device_readings 用 EAV（一个指标一行）
-------------------------------------------
一条消息里可能同时有温度和湿度（统一信封的 `metrics`），
而且将来会加指标。用宽表就得频繁改表结构，用 EAV 则"加指标零成本"，
查询也直观：`WHERE metric_code='temperature'`。

值编码规则
----------
- **数值**（含 bool） → `value_num`  —— bool 编成 1.0 / 0.0，
  这样"空调开了多久"是 `AVG(value_num)`，能直接进聚合函数。
- **字符串 / 枚举**  → `value_text` —— 如 `mode='cool'`、`lock_state='locked'`。

⚠️ 两个刻意的行为
1. **坏数据也要入库**（打上 quality / is_stale 标记）——不入库就没法做质量复盘，
   也不符合"审计留痕"的要求；污染问题靠标记 + 下游过滤解决，不靠丢弃。
2. **未知设备拒收** —— 不在 `devices` 表里的 code 不写（否则外键报错，
   或者更糟：凭空造出数据）。MQTT.fx 手工注入请用库里已有的设备 code。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.core.tariff import Tariff, get_tariff
from app.db.catalog import DeviceCatalog, DeviceInfo
from app.db.session import SessionLocal
from app.models.orm import DeviceReading, EnergyReading, SensorEvent
from app.mqtt.cache import LatestValueCache
from app.mqtt.envelope import (
    READING_KINDS,
    Envelope,
    EnvelopeError,
    parse,
    to_local_naive,
)
from app.mqtt.quality import QualityEvaluator

log = logging.getLogger("app.mqtt.ingest")

#: 触发"额外记一条能耗"的指标名
POWER_METRIC = "power_w"

#: 能量积分的最大步长（秒）。超过它说明中间断了很久，
#: 那段功率是未知的 —— 宁可少记，也不要凭空插值算出一个假电量。
MAX_ENERGY_GAP_SEC = 1800.0

EVENT_LEVELS = ("info", "warning", "critical")

#: 同一设备同一事件在多少秒内重复到达时只记一次
EVENT_DEDUP_WINDOW_SEC = 60.0


def encode_value(value: Any) -> tuple[float | None, str | None]:
    """把一个 metric 值编码成 (value_num, value_text)。"""
    if isinstance(value, bool):
        return (1.0 if value else 0.0), None
    if isinstance(value, (int, float)):
        return float(value), None
    if value is None:
        return None, None
    return None, str(value)[:128]


@dataclasses.dataclass
class PendingBatch:
    """待写库的一批行。"""

    readings: list[dict] = dataclasses.field(default_factory=list)
    energies: list[dict] = dataclasses.field(default_factory=list)
    events: list[dict] = dataclasses.field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.readings or self.energies or self.events)

    def __len__(self) -> int:
        return len(self.readings) + len(self.energies) + len(self.events)


@dataclasses.dataclass
class IngestStats:
    received: int = 0
    rejected: int = 0          # topic / payload 不合格
    unknown_device: int = 0
    control_ignored: int = 0
    duplicates: int = 0
    stale: int = 0
    out_of_range: int = 0
    retained_snapshots: int = 0   # broker 回放的历史状态快照（会话初始化用）
    readings: int = 0
    energies: int = 0
    events: int = 0
    event_deduped: int = 0
    flushes: int = 0
    errors: int = 0
    last_payload_at: dt.datetime | None = None
    last_flush_at: dt.datetime | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Ingestor:
    """把 MQTT 消息转成数据库行。

    线程模型（很重要）
    ------------------
    - `handle()` 是**同步**的，只在 asyncio 事件循环里被调用（CPU 级开销，很快）；
    - `write_batch()` 是**同步**的，会被丢到线程池执行（远程库往返慢，不能阻塞事件循环）；
    - 两者通过 `take_batch()` 交换数据：它把当前缓冲**整体换出**，
      下一步 `handle()` 往新缓冲写 —— 没有共享可变状态，因此不需要锁。
    """

    def __init__(
        self,
        catalog: DeviceCatalog,
        tz: dt.tzinfo,
        tariff: Tariff | None = None,
        *,
        cache: LatestValueCache | None = None,
        energy_integration: bool = True,
    ):
        self.catalog = catalog
        self.tz = tz
        self.tariff = tariff if tariff is not None else get_tariff()
        self.energy_integration = energy_integration

        self.quality = QualityEvaluator(catalog)

        # ⚠️ 缓存允许外部注入，而且**在 API 进程里必须注入**。
        # 订阅线程写缓存、API 端点读缓存，两者必须是**同一个实例**；
        # 各自 new 一个的后果极其隐蔽：健康检查能看到"MQTT 收了 1000+ 条消息、
        # 数据也在正常入库"，但接口读到的永远是启动时的预热值、所有设备都显示离线
        # —— 两边单看都"在工作"，只是根本没接上。（实测踩过这个坑）
        self.cache = cache if cache is not None else LatestValueCache()

        self.stats = IngestStats()

        self._pending = PendingBatch()
        self._last_energy_at: dict[str, dt.datetime] = {}
        self._last_event_at: dict[tuple[str, str], dt.datetime] = {}

    # ================================================================== 入口
    def handle(self, topic: str, payload: bytes | str, *, retain: bool = False) -> bool:
        """处理一条 MQTT 消息。返回是否被接受入库。

        `retain=True` 表示这是 broker 回放的**历史状态快照**（订阅瞬间一次性推送）：
        照常入库，但不参与"时间倒退"判定，也不更新时间基准 —— 理由见
        `quality.QualityEvaluator.evaluate`。

        任何异常都在这里被吃掉 —— 一条坏消息绝不能把订阅循环打挂。
        """
        self.stats.received += 1
        self.stats.last_payload_at = dt.datetime.now()

        try:
            ref, env = parse(topic, payload)
        except EnvelopeError as e:
            self.stats.rejected += 1
            log.warning("拒收消息 topic=%s 原因=%s", topic, e)
            return False

        # 控制类消息（cmd / cmd_ack）不属于观测数据，本链路不处理。
        # 高风险指令有专门的待确认通道，见规划书；这里只计数以便排障。
        if env.is_control:
            self.stats.control_ignored += 1
            return False

        device = self.catalog.get(env.device_code)
        if device is None:
            self.stats.unknown_device += 1
            log.warning("未知设备 %s（不在 devices 表里，拒绝入库）", env.device_code)
            return False

        observed_local = to_local_naive(env.observed_at, self.tz)
        verdict = self.quality.evaluate(env, observed_local, device, retain=retain)

        if not verdict.accepted:
            self.stats.duplicates += 1
            return False

        if retain:
            self.stats.retained_snapshots += 1
        if verdict.is_stale:
            self.stats.stale += 1
        if verdict.out_of_range:
            self.stats.out_of_range += 1

        # 最新值缓存：只收 good 且非 stale（"最新值"必须可信）
        # received_at 用本机时间 —— 在线判定必须看"我们什么时候收到的"，
        # 而不是消息里的观测时间（那是设备时钟，可能不准甚至在"未来"）。
        # retained 快照不记为"在线"证据：它是 broker 回放的历史状态，不代表设备在发数据。
        self.cache.update(
            device.code,
            env.metrics,
            observed_local,
            units=env.units,
            quality=verdict.quality,
            is_stale=verdict.is_stale,
            seq=env.seq,
            received_at=dt.datetime.now(),
            count_as_live=not retain,
        )

        if env.is_event:
            self._stage_event(env, device, observed_local, verdict)
        elif env.kind in READING_KINDS:
            self._stage_readings(env, device, observed_local, verdict)
            self._stage_energy(env, device, observed_local, verdict)

        return True

    # ================================================================ 组装行
    def _stage_readings(
        self, env: Envelope, device: DeviceInfo, observed_local: dt.datetime, verdict
    ) -> None:
        for metric, value in env.metrics.items():
            num, text = encode_value(value)
            self._pending.readings.append(
                {
                    "device_id": device.id,
                    "observed_at": observed_local,
                    "metric_code": metric,
                    "value_num": num,
                    "value_text": text,
                    "unit": env.units.get(metric),
                    "quality": verdict.quality,
                    "is_stale": verdict.is_stale,
                    "seq": env.seq,
                    "source": env.source,
                }
            )
        self.stats.readings += len(env.metrics)

    def _stage_energy(
        self, env: Envelope, device: DeviceInfo, observed_local: dt.datetime, verdict
    ) -> None:
        """功率读数额外记一条能耗明细（含增量电量与峰平谷标签）。"""
        if not self.energy_integration or POWER_METRIC not in env.metrics:
            return

        try:
            power = float(env.metrics[POWER_METRIC])
        except (TypeError, ValueError):
            return

        energy_kwh: float | None = None
        prev = self._last_energy_at.get(device.code)
        if prev is not None:
            gap = (observed_local - prev).total_seconds()
            # gap <= 0：时间倒退（补发数据）→ 不计，否则会算出负电量
            # gap > MAX：中间断连太久 → 那段功率未知，不凭空补
            if 0 < gap <= MAX_ENERGY_GAP_SEC:
                energy_kwh = power * gap / 3600.0 / 1000.0

        if prev is None or observed_local > prev:
            self._last_energy_at[device.code] = observed_local

        band = self.tariff.band_at(observed_local) if self.tariff else None
        self._pending.energies.append(
            {
                "device_id": device.id,
                "observed_at": observed_local,
                "power_w": power,
                "energy_kwh": energy_kwh,
                "tariff_period": band.period if band else None,
                "source": env.source,
            }
        )
        self.stats.energies += 1

    def _stage_event(
        self, env: Envelope, device: DeviceInfo, observed_local: dt.datetime, verdict
    ) -> None:
        event_code = env.event_code or "unknown_event"

        # 同一设备同一事件短时间内重复到达 → 只记一次（避免告警风暴）
        key = (device.code, event_code)
        prev = self._last_event_at.get(key)
        if prev is not None and abs((observed_local - prev).total_seconds()) < EVENT_DEDUP_WINDOW_SEC:
            self.stats.event_deduped += 1
            return
        self._last_event_at[key] = observed_local

        level = (env.level or "warning").lower()
        if level not in EVENT_LEVELS:
            level = "warning"

        self._pending.events.append(
            {
                "device_id": device.id,
                "event_code": event_code,
                "level": level,
                "started_at": observed_local,
                "ended_at": None,           # 事件结束需要后续判定，首期先留空
                "payload": env.model_dump(mode="json"),   # 原样存档，便于追溯
                "source": env.source,
            }
        )
        self.stats.events += 1

    # ================================================================ 批量写
    def take_batch(self) -> PendingBatch:
        """把当前缓冲整体换出（原子操作，无 await 点，因此不需要锁）。"""
        batch, self._pending = self._pending, PendingBatch()
        return batch

    def write_batch(self, batch: PendingBatch, session: Session | None = None) -> int:
        """把一批行写进库。**在线程池里调用**，不要直接在事件循环里 await 它。"""
        if batch.is_empty:
            return 0

        own_session = session is None
        db = session or SessionLocal()
        try:
            if batch.readings:
                db.execute(insert(DeviceReading), batch.readings)
            if batch.energies:
                db.execute(insert(EnergyReading), batch.energies)
            if batch.events:
                db.execute(insert(SensorEvent), batch.events)
            if own_session:
                db.commit()
            return len(batch)
        except Exception as e:  # noqa: BLE001
            if own_session:
                db.rollback()
            self.stats.errors += 1
            self.stats.last_error = f"{type(e).__name__}: {e}"
            log.exception("批量写库失败（%d 行）", len(batch))
            raise
        finally:
            if own_session:
                db.close()

    def on_flushed(self, rows: int) -> None:
        self.stats.flushes += 1
        self.stats.last_flush_at = dt.datetime.now()
