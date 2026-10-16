"""设备类工具：查现状 / 查能力 / 查历史。

⚠️ 三个工具都是**只读**的。本项目里"改设备"只有一个入口
（`action_tools.propose_scene_actions`），而且它也只产出建议、不执行。

工具为什么用同步 `def`
--------------------
LangChain 在异步 Agent 里执行同步工具时会自动丢到线程池（`run_in_executor`），
所以同步工具不会阻塞事件循环。而同步 SQLAlchemy 直接写在 `async def` 里
**会**阻塞 —— 这是本项目在 M2 就定下的纪律。

每个工具自己开短会话（`SessionLocal()`）而不是共享一个 `db`：
Session 不是线程安全的，而工具可能跑在不同线程里。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from langchain_core.tools import tool

from app.db.catalog import DeviceCatalog, DeviceInfo
from app.db.session import SessionLocal
from app.services.overview import DEFAULT_OFFLINE_AFTER_SEC

#: 房间名 / 设备类型的中文别名。模型经常中英文混着说，
#: 在这里宽容一点比让它反复试错便宜。
_DEVICE_TYPE_ALIAS: dict[str, str] = {
    "灯": "light", "照明": "light",
    "窗帘": "curtain",
    "空调": "ac",
    "电视": "tv",
    "音箱": "speaker",
    "加湿器": "humidifier",
    "热水器": "water_heater",
    "门锁": "door_lock",
    "门磁": "door_sensor",
    "总闸": "main_breaker", "配电箱": "main_breaker",
    "燃气阀": "gas_valve",
    "燃气传感器": "gas_sensor", "燃气": "gas_sensor",
    "烟感": "smoke_sensor", "烟雾": "smoke_sensor",
    "水浸": "water_leak_sensor",
    "温湿度": "temp_sensor", "温度": "temp_sensor",
    "空气质量": "air_sensor",
    "毫米波": "mmwave_radar", "雷达": "mmwave_radar",
    "摄像头": "camera",
}


def _resolve_room_code(catalog: DeviceCatalog, room: str | None) -> str | None:
    """把房间名解析成 room_code（同时接受 living_room 与「客厅」）。"""
    if not room:
        return None
    token = room.strip()
    if catalog.room(token) is not None:
        return token
    for r in catalog.rooms:
        if token == r.name or token in r.name or r.name in token:
            return r.code
    return None


def _resolve_device_type(catalog: DeviceCatalog, device_type: str | None) -> str | None:
    """把设备类型的中文/英文写法统一成库里的 device_type。"""
    if not device_type:
        return None
    token = device_type.strip()
    known = {d.device_type for d in catalog.all()}
    if token in known:
        return token
    for k, v in _DEVICE_TYPE_ALIAS.items():
        if k in token:
            return v
    return None


def _find_devices(
    catalog: DeviceCatalog,
    *,
    room: str | None,
    device_type: str | None,
    device_code: str | None,
) -> tuple[list[DeviceInfo], str | None]:
    """返回 (设备列表, 错误说明)。错误说明非空表示参数无法解析。

    报错时**明确告诉模型有什么可选**，比只回一句"没找到"更有效 ——
    否则它会开始编设备名。
    """
    if device_code:
        dev = catalog.get(device_code.strip())
        if dev is None:
            return [], f"设备编码 {device_code!r} 不存在。请勿编造设备，可用清单以数据库为准。"
        return [dev], None

    room_code = _resolve_room_code(catalog, room)
    if room and room_code is None:
        names = "、".join(r.name for r in catalog.rooms)
        return [], f"房间 {room!r} 不存在。共有这些房间：{names}"

    dtype = _resolve_device_type(catalog, device_type)
    if device_type and dtype is None:
        types = sorted({d.device_type for d in catalog.all()})
        return [], f"设备类型 {device_type!r} 不认识。已登记的类型：{', '.join(types)}"

    devs = catalog.all()
    if room_code:
        devs = [d for d in devs if d.room_code == room_code]
    if dtype:
        devs = [d for d in devs if d.device_type == dtype]
    return devs, None


def _freshness(observed_at: dt.datetime | None, now: dt.datetime) -> str:
    """把"数据有多旧"说成人话。

    缓存里存的是**最后一次质量合格的上报值**，所以值本身可信；
    但如果设备很久没上报，这个值对"现在"就没有意义了 ——
    这一点必须显式告知模型，否则它会把两小时前的温度当成实时值。
    """
    if observed_at is None:
        return ""
    age = (now - observed_at).total_seconds()
    if age <= DEFAULT_OFFLINE_AFTER_SEC:
        return ""
    if age < 3600:
        return f" ⚠️已 {int(age / 60)} 分钟未更新"
    return f" ⚠️已 {age / 3600:.1f} 小时未更新"


def build_device_tools(ctx: Any) -> list[Any]:
    """构造设备类工具（绑定当前进程的设备目录与最新值缓存）。"""
    catalog: DeviceCatalog = ctx.catalog
    cache = ctx.cache

    @tool
    def read_device_state(
        room: str | None = None,
        device_type: str | None = None,
        device_code: str | None = None,
    ) -> str:
        """查询设备的**当前状态**（温度、湿度、开关、亮度、功率、在线状态等）。

        什么时候用：用户问"现在怎么样""开着吗""多少度""客厅有什么设备"这类**现状**问题。

        参数（三个都可以留空；都不给则返回全屋所有设备）：
        - room        房间名，中文（客厅）或编码（living_room）都行
        - device_type 设备类型，中文（灯/空调/温湿度）或英文（light/ac/temp_sensor）都行
        - device_code 精确到某台设备的编码

        返回每台设备的当前值与更新时间。**若某条数据被标注"未更新"，必须在回答里
        如实说明这一点，不得把它当作实时值陈述。**
        """
        devs, err = _find_devices(catalog, room=room, device_type=device_type, device_code=device_code)
        if err:
            return err
        if not devs:
            return "该条件下没有查到设备。请勿编造设备。"

        now = dt.datetime.now()
        lines: list[str] = []
        for dev in sorted(devs, key=lambda d: (d.room_code, d.id)):
            latest = cache.device(dev.code)
            flag = "  ⚠️高风险设备(不可自动执行)" if dev.high_risk else ""
            lines.append(f"{dev.room_name} · {dev.name}（{dev.code}）{flag}")
            if not latest:
                lines.append("    · 暂无数据（该设备还没有上报过）")
                continue
            for metric, item in latest.items():
                unit = item.unit or ""
                when = item.observed_at.isoformat(timespec="seconds") if item.observed_at else "?"
                lines.append(
                    f"    · {metric} = {item.value}{unit}"
                    f"（更新于 {when}{_freshness(item.observed_at, now)}）"
                )
        lines.append(f"（读取时刻 {now.isoformat(timespec='seconds')}）")
        return "\n".join(lines)

    @tool
    def list_capability(
        room: str | None = None,
        device_type: str | None = None,
        device_code: str | None = None,
    ) -> str:
        """查询设备**支持哪些能力、取值范围是多少**。

        ⚠️ **生成任何控制指令之前必须先调用本工具**，用来确认设备到底能不能做这件事、
        参数允许到多少。不要在未调用本工具时假设设备具备某个功能。

        参数含义与 read_device_state 相同。返回每个能力的类型、范围与是否可写。
        """
        devs, err = _find_devices(catalog, room=room, device_type=device_type, device_code=device_code)
        if err:
            return err
        if not devs:
            return "该条件下没有查到设备。不得对其下发控制指令。"

        lines: list[str] = []
        for dev in sorted(devs, key=lambda d: (d.room_code, d.id)):
            if not dev.capabilities:
                lines.append(f"{dev.name}（{dev.code}）：无已登记能力档案，**不得下发控制指令**")
                continue
            flag = "  ⚠️高风险" if dev.high_risk else ""
            lines.append(f"{dev.room_name} · {dev.name}（{dev.code}）{flag}")
            for cap in dev.capabilities.values():
                rng = ""
                if cap.enum_values:
                    rng = f" 可选: {'/'.join(cap.enum_values)}"
                elif cap.value_min is not None and cap.value_max is not None:
                    rng = f" 范围: {cap.value_min}~{cap.value_max}{cap.unit or ''}"
                elif cap.unit:
                    rng = f" 单位: {cap.unit}"
                w = "可写" if cap.writable else "只读"
                lines.append(
                    f"    · {cap.code}（{cap.display_name or cap.code}, {cap.value_type}, {w}）{rng}"
                )
        return "\n".join(lines)

    @tool
    def read_history(device_code: str, hours: int = 24, metric: str | None = None) -> str:
        """查询某台设备的**历史读数**（用于看趋势、找异常、做对比）。

        什么时候用：用户问"今天温度怎么变化""最近有没有异常""对比一下"这类需要
        **时间序列**的问题。只问"现在多少"请用 read_device_state。

        参数：
        - device_code 设备编码（必填）
        - hours       往前查多少小时，默认 24
        - metric      只查某个指标（如 temperature / power_w），留空则返回全部指标

        返回每个指标的统计（最小/最大/平均/点数）与最近几条原始点。
        """
        devs, err = _find_devices(catalog, room=None, device_type=None, device_code=device_code)
        if err:
            return err
        device = devs[0]

        try:
            span_hours = max(1, min(int(hours or 24), 24 * 31))
        except (TypeError, ValueError):
            span_hours = 24

        from app.services import analytics

        with SessionLocal() as db:
            to_dt = dt.datetime.now()
            from_dt = to_dt - dt.timedelta(hours=span_hours)
            try:
                data = analytics.device_readings(
                    db, device, from_dt=from_dt, to_dt=to_dt,
                    metrics=[metric] if metric else None,
                )
            except Exception as e:  # noqa: BLE001
                return f"查询历史失败：{type(e).__name__}: {e}"

        series = data.get("metrics") or {}
        if not series:
            return (
                f"{device.name}（{device.code}）在最近 {span_hours} 小时内没有历史数据。"
                "请如实告知用户没有数据，不要推测。"
            )
        lines = [f"{device.name}（{device.code}）最近 {span_hours} 小时："]
        for m, info in series.items():
            pts = info.get("points") or []
            values = [p["value"] for p in pts if p.get("value") is not None]
            if not values:
                lines.append(f"  · {m}：无有效值")
                continue
            unit = info.get("unit") or ""
            avg = sum(values) / len(values)
            lines.append(
                f"  · {m}：{len(values)} 个点，最小 {min(values):.2f}{unit}，"
                f"最大 {max(values):.2f}{unit}，平均 {avg:.2f}{unit}"
            )
            tail = pts[-3:]
            lines.append(
                "      最近：" + " → ".join(
                    f"{p['value']}{unit}@{str(p.get('observed_at'))[11:19]}" for p in tail
                )
            )
        lines.append(f"  （共 {data.get('row_count', 0)} 行原始记录）")
        return "\n".join(lines)

    return [read_device_state, list_capability, read_history]
