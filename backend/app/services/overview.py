"""态势查询：房间卡片、设备列表、设备能力。

设计原则
--------
1. **能用内存就用内存**：设备 / 房间 / 能力来自 `DeviceCatalog`，当前值来自
   `LatestValueCache`。只有"告警摘要"这类事务数据才打库，而且是**一条查询覆盖所有房间**
   （不做 N+1——前端总览页每几秒刷新一次，32 台设备逐个查库很快就成了瓶颈）。
2. **不猜数据**：缓存里没有的指标就是"没有"，不返回 0 或空串冒充。
   前端看到 `latest: {}` 就知道"这台设备当前没有可信读数"，
   而不是把 0 当成"温度 0℃"。这一点直接对应规划书"数据不足不得编造"的红线。
3. **元数据必须一起返回**：每个值都带 `observed_at / quality / unit`，
   否则前端无法判断"这个数字是不是已经过期了"。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.catalog import DeviceCatalog, DeviceInfo
from app.mqtt.cache import LatestValueCache

#: 房间卡片上优先展示哪些指标（越靠前越先展示）。
#: 顺序反映"人对一个房间最想知道什么"：先温湿度，再空气质量与安全类，最后设备状态。
KEY_METRIC_PRIORITY: tuple[str, ...] = (
    "temperature", "humidity",
    "pm25", "co2",
    "gas_concentration", "smoke_detected", "water_detected",
    "presence", "activity_level", "open",
    "power_w", "power_on", "brightness", "position",
    "lock_state", "valve_state", "power_state",
)

#: 超过这个时间没上报就算"离线"。设备是秒级上报的，10 分钟没动静基本就是有问题。
DEFAULT_OFFLINE_AFTER_SEC = 600.0

#: 房间卡片上最多展示几个关键指标
DEFAULT_KEY_METRIC_LIMIT = 4

#: 设备关闭后就**不再有意义**的指标：指标名 → 它依赖的开关量。
#:
#: ⚠️ 为什么必须过滤：缓存存的是"每个指标的最后一次上报值"。灯关掉之后模拟器
#: 不再上报 brightness，但缓存里还留着关灯前最后一次的值（可能已经过去几十分钟）。
#: 直接展示就成了"灯关着，房间却还亮着 66%"这种自相矛盾的数据 ——
#: 用户看到的第一反应是"这系统乱报"，而不是"这是旧值"。
#: 所以开关明确为"关"时，这些指标一律不进房间读数。
OFF_MEANS_NO_VALUE: dict[str, str] = {
    "brightness": "power_on",
}


# ---------------------------------------------------------------- 内部工具
def _latest_payload(cache: LatestValueCache, dev: DeviceInfo) -> dict[str, dict[str, Any]]:
    """把缓存里这台设备的值整理成可序列化的结构。

    单位以**能力档案**为准（它才是设备能力的唯一事实来源），缓存里的单位只作兜底 ——
    因为能力档案有 `unit` 字段而实时消息里的 `units` 是可选的。
    """
    out: dict[str, dict[str, Any]] = {}
    for metric, lv in cache.device(dev.code).items():
        cap = dev.cap(metric)
        out[metric] = {
            "value": lv.value,
            "unit": (cap.unit if cap and cap.unit else lv.unit),
            "observed_at": lv.observed_at,
            "quality": lv.quality,
            "seq": lv.seq,
        }
    return out


def _online_state(
    cache: LatestValueCache,
    dev: DeviceInfo,
    now: dt.datetime,
    offline_after_sec: float,
) -> tuple[bool, dt.datetime | None, float | None]:
    last = cache.last_seen(dev.code)
    if last is None:
        return False, None, None
    age = (now - last).total_seconds()
    return age <= offline_after_sec, last, round(age, 1)


def _event_counts_by_room(db: Session, since: dt.datetime) -> dict[int, int]:
    rows = db.execute(
        text(
            """
            SELECT r.id AS room_id, COUNT(*) AS n
            FROM sensor_events e
            JOIN devices d ON d.id = e.device_id
            JOIN rooms r ON r.id = d.room_id
            WHERE e.started_at >= :since
            GROUP BY r.id
            """
        ),
        {"since": since},
    ).all()
    return {r.room_id: r.n for r in rows}


def _alarm_counts_by_room(db: Session) -> dict[int, int]:
    """当前仍处于 active 的告警数（按房间）。

    ⚠️ 只数 `alarms` 表里真实存在的活动告警。告警的产生属于安防链路（后续里程碑），
    这里**不会**因为"有个传感器报过异常"就编一条告警出来。
    """
    rows = db.execute(
        text(
            """
            SELECT room_id, COUNT(*) AS n
            FROM alarms
            WHERE status = 'active' AND room_id IS NOT NULL
            GROUP BY room_id
            """
        )
    ).all()
    return {r.room_id: r.n for r in rows}


# ---------------------------------------------------------------- 房间卡片
def room_cards(
    db: Session,
    catalog: DeviceCatalog,
    cache: LatestValueCache,
    home_id: int,
    *,
    now: dt.datetime | None = None,
    offline_after_sec: float = DEFAULT_OFFLINE_AFTER_SEC,
    key_metric_limit: int = DEFAULT_KEY_METRIC_LIMIT,
    event_window_hours: int = 24,
) -> list[dict[str, Any]]:
    """家庭总览用的房间卡片墙。"""
    now = now or dt.datetime.now()
    event_counts = _event_counts_by_room(db, now - dt.timedelta(hours=event_window_hours))
    alarm_counts = _alarm_counts_by_room(db)

    cards: list[dict[str, Any]] = []
    for room in catalog.rooms_of(home_id):
        devs = catalog.in_room(room.code)

        online = 0
        last_updates: list[dt.datetime] = []
        candidates: list[tuple[int, DeviceInfo, str, Any]] = []

        for dev in devs:
            is_online, last, _age = _online_state(cache, dev, now, offline_after_sec)
            if is_online:
                online += 1
            # "数据更新时间"用**观测时间**（含预热数据），不是实时上报时间 ——
            # 否则重启后明明手里有数据，卡片上却显示"从未更新"。
            observed = cache.observed_extent(dev.code)
            if observed is not None:
                last_updates.append(observed)
            latest = cache.device(dev.code)
            for metric, lv in latest.items():
                if metric not in KEY_METRIC_PRIORITY:
                    continue
                # 灯关了还显示"亮度"，是缓存里留着关灯前的旧值（见 OFF_MEANS_NO_VALUE）
                gate = OFF_MEANS_NO_VALUE.get(metric)
                if gate:
                    sw = latest.get(gate)
                    # 只有设备**明确上报过关闭**才丢弃；没上报过开关量就不猜
                    if sw is not None and sw.value in (False, 0, "off", "closed"):
                        continue
                candidates.append((KEY_METRIC_PRIORITY.index(metric), dev, metric, lv))

        candidates.sort(key=lambda item: item[0])
        key_metrics = [
            {
                "metric_code": metric,
                "value": lv.value,
                "unit": (dev.cap(metric).unit if dev.cap(metric) else lv.unit),
                "device_id": dev.id,
                "device_name": dev.name,
                "observed_at": lv.observed_at,
                "quality": lv.quality,
            }
            for _prio, dev, metric, lv in candidates[:key_metric_limit]
        ]

        cards.append(
            {
                "room_id": room.id,
                "code": room.code,
                "name": room.name,
                "sort_no": room.sort_no,
                "device_count": len(devs),
                "online_count": online,
                "offline_count": len(devs) - online,
                "high_risk_count": sum(1 for d in devs if d.high_risk),
                "active_alarm_count": alarm_counts.get(room.id, 0),
                "recent_event_count": event_counts.get(room.id, 0),
                "last_update_at": max(last_updates) if last_updates else None,
                "key_metrics": key_metrics,
            }
        )
    return cards


# ---------------------------------------------------------------- 设备列表
def device_list(
    catalog: DeviceCatalog,
    cache: LatestValueCache,
    *,
    room_code: str | None = None,
    device_type: str | None = None,
    codes: list[str] | None = None,
    high_risk_only: bool = False,
    online_only: bool = False,
    home_id: int | None = None,
    now: dt.datetime | None = None,
    offline_after_sec: float = DEFAULT_OFFLINE_AFTER_SEC,
) -> list[dict[str, Any]]:
    """设备列表（按房间顺序排列）。当前值来自缓存，不查库。

    `home_id` 为空表示不按套房过滤（给全屋统计用）；页面一律要传，
    否则会把别的房子的设备列出来 —— 多套房下这是最容易被忽略的越权式串台。
    """
    now = now or dt.datetime.now()
    room_order = {r.code: r.sort_no for r in catalog.rooms}
    devices = sorted(catalog.all(), key=lambda d: (room_order.get(d.room_code, 999), d.id))
    code_filter = set(codes) if codes is not None else None

    out: list[dict[str, Any]] = []
    for dev in devices:
        if home_id is not None and dev.home_id != home_id:
            continue
        if code_filter is not None and dev.code not in code_filter:
            continue
        if room_code and dev.room_code != room_code:
            continue
        if device_type and dev.device_type != device_type:
            continue
        if high_risk_only and not dev.high_risk:
            continue

        is_online, last, age = _online_state(cache, dev, now, offline_after_sec)
        if online_only and not is_online:
            continue

        out.append(
            {
                "id": dev.id,
                "code": dev.code,
                "name": dev.name,
                "device_type": dev.device_type,
                "room_code": dev.room_code,
                "room_name": dev.room_name,
                "high_risk": dev.high_risk,
                # 高风险设备永不自动执行 —— 前端据此决定是否显示"需人工确认"
                "auto_exec_allowed": not dev.high_risk,
                "online": is_online,
                # ⚠️ 两个时间语义不同，前端要分开用：
                #   last_seen        = 数据源最后一次说话（在线判定依据，服务刚起时可能为空）
                #   last_observed_at = 手上最新数据的观测时间（含启动预热，用于显示"数据更新时间"）
                "last_seen": last,
                "last_observed_at": cache.observed_extent(dev.code),
                "data_age_sec": age,
                "capability_count": len(dev.capabilities),
                "writable_count": sum(1 for c in dev.capabilities.values() if c.writable),
                "latest": _latest_payload(cache, dev),
            }
        )
    return out


# ---------------------------------------------------------------- 设备能力
def device_capabilities(dev: DeviceInfo) -> dict[str, Any]:
    """能力档案明细 —— 模型与前端判断"这台设备能不能做某件事"的唯一依据。"""
    return {
        "device_id": dev.id,
        "code": dev.code,
        "name": dev.name,
        "device_type": dev.device_type,
        "room_code": dev.room_code,
        "room_name": dev.room_name,
        "high_risk": dev.high_risk,
        "auto_exec_allowed": not dev.high_risk,
        "capabilities": [
            {
                "code": cap.code,
                "display_name": cap.display_name,
                "value_type": cap.value_type,
                "unit": cap.unit,
                "value_min": cap.value_min,
                "value_max": cap.value_max,
                "enum_values": list(cap.enum_values),
                "writable": cap.writable,
                "risk_level": cap.risk_level,
            }
            for cap in dev.capabilities.values()
        ],
    }


def device_summary(catalog: DeviceCatalog, home_id: int | None = None) -> dict[str, int]:
    """设备总览数字（房间卡片刻意不含这些，放在 /api/devices 的元信息里）。

    `home_id` 为空 = 全部套房；页面传当前套房，否则底部的"32/32 在线"会把
    别人的设备也算进去。
    """
    devices = [d for d in catalog.all() if home_id is None or d.home_id == home_id]
    rooms = [r for r in catalog.rooms if home_id is None or r.home_id == home_id]
    return {
        "devices": len(devices),
        "rooms": len(rooms),
        "high_risk": sum(1 for d in devices if d.high_risk),
        "capabilities": sum(len(d.capabilities) for d in devices),
    }


# ---------------------------------------------------------------- 数据时间锚点
def data_anchor(catalog: DeviceCatalog, cache: LatestValueCache) -> dt.datetime | None:
    """数据时间锚点 = 手上最新数据的观测时间。

    为什么需要它（这是模拟数据项目最容易踩的坑之一）
    ----------------------------------------------
    本项目的数据来自**模拟器**，它的时间轴与真实时钟不一定一致：
    按倍速推进、或从某个历史时刻开始跑，跑一阵子数据时间就跑到"未来"去了。

    此时如果 API 的缺省时间窗是"现在往前 24 小时"，会**一条都查不到** ——
    用户看到"没有数据"，而数据其实好好地躺在库里。这类"看起来没坏但用不了"的问题最难查。

    所以缺省时间窗以**数据自身的最新时间**为基准：

    - 真实部署里数据时间 ≈ 现在，行为与"以 now 为基准"完全一致；
    - 模拟数据场景下也能正确查到数据。

    返回 `None` 表示手上没有任何数据（调用方用 now 兜底）。
    """
    latest: dt.datetime | None = None
    for dev in catalog.all():
        ts = cache.observed_extent(dev.code)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest
