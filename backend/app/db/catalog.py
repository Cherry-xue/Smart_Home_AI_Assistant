"""设备目录（进程内缓存）：code ↔ id 映射 + 能力取值范围。

为什么要在内存里缓存
--------------------
MQTT 每秒会进来几十条消息。如果每来一条都去查一次"这台设备存在吗、
这个指标的合法范围是多少"，远程库里外网往返会把整条链路拖垮。

而 `devices` / `device_capabilities` 是**档案**性质的数据 —— 变更频率极低
（只在改设备清单时变），所以进程启动时加载一次即可。档案改了重启进程。

顺带一个安全收益：**只有目录里登记过的 device_code 才允许入库**，
MQTT.fx 手工注入一个不存在的设备不会被写进库里（否则外键会报错、
或者更糟：凭空造出设备数据）。
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclasses.dataclass(frozen=True)
class CapabilityRange:
    code: str
    display_name: str
    value_type: str
    unit: str | None
    value_min: float | None
    value_max: float | None
    enum_values: tuple[str, ...]
    writable: bool
    risk_level: str

    def contains(self, value: object) -> bool:
        """值是否落在能力档案声明的范围内（无范围的类型一律放行）。"""
        if self.value_min is None or self.value_max is None:
            return True
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True
        return float(self.value_min) <= float(value) <= float(self.value_max)


@dataclasses.dataclass(frozen=True)
class RoomInfo:
    id: int
    code: str
    name: str
    sort_no: int
    home_id: int


@dataclasses.dataclass(frozen=True)
class DeviceInfo:
    id: int
    code: str
    name: str
    device_type: str
    home_id: int
    room_code: str
    room_name: str
    high_risk: bool
    capabilities: dict[str, CapabilityRange]

    def cap(self, metric: str) -> CapabilityRange | None:
        return self.capabilities.get(metric)


class DeviceCatalog:
    """设备 + 能力档案 + 房间的只读快照。"""

    def __init__(self, devices: dict[str, DeviceInfo], rooms: list[RoomInfo]):
        self._by_code = devices
        self._by_id = {d.id: d for d in devices.values()}
        self._rooms = rooms
        self._rooms_by_code = {r.code: r for r in rooms}

    # ---------------------------------------------------------------- 加载
    @classmethod
    def load(cls, engine: Engine) -> DeviceCatalog:
        with engine.connect() as conn:
            room_rows = conn.execute(
                text(
                    """
                    SELECT id, code, name, sort_no, home_id
                    FROM rooms
                    ORDER BY home_id, sort_no
                    """
                )
            ).all()

            dev_rows = conn.execute(
                text(
                    """
                    SELECT d.id, d.code, d.name, d.device_type, d.home_id, d.high_risk,
                           r.code AS room_code, r.name AS room_name
                    FROM devices d
                    LEFT JOIN rooms r ON r.id = d.room_id
                    WHERE d.enabled = 1
                    ORDER BY d.id
                    """
                )
            ).all()

            cap_rows = conn.execute(
                text(
                    """
                    SELECT dc.device_id, dc.capability_code, dc.display_name, dc.value_type,
                           dc.unit, dc.value_min, dc.value_max, dc.enum_values,
                           dc.writable, dc.risk_level
                    FROM device_capabilities dc
                    JOIN devices d ON d.id = dc.device_id
                    WHERE d.enabled = 1
                    ORDER BY dc.device_id, dc.id
                    """
                )
            ).all()

        caps: dict[int, dict[str, CapabilityRange]] = {}
        for r in cap_rows:
            caps.setdefault(r.device_id, {})[r.capability_code] = CapabilityRange(
                code=r.capability_code,
                display_name=r.display_name,
                value_type=r.value_type,
                unit=r.unit,
                value_min=float(r.value_min) if r.value_min is not None else None,
                value_max=float(r.value_max) if r.value_max is not None else None,
                enum_values=tuple(x for x in (r.enum_values or "").split(",") if x),
                writable=bool(r.writable),
                risk_level=r.risk_level,
            )

        devices = {
            r.code: DeviceInfo(
                id=r.id,
                code=r.code,
                name=r.name,
                device_type=r.device_type,
                home_id=r.home_id,
                room_code=r.room_code or "",
                room_name=r.room_name or "",
                high_risk=bool(r.high_risk),
                capabilities=caps.get(r.id, {}),
            )
            for r in dev_rows
        }
        rooms = [
            RoomInfo(id=r.id, code=r.code, name=r.name, sort_no=r.sort_no, home_id=r.home_id)
            for r in room_rows
        ]
        return cls(devices, rooms)

    # ---------------------------------------------------------------- 查询
    def get(self, code: str) -> DeviceInfo | None:
        return self._by_code.get(code)

    def by_id(self, device_id: int) -> DeviceInfo | None:
        return self._by_id.get(device_id)

    def all(self) -> list[DeviceInfo]:
        return list(self._by_code.values())

    def in_room(self, room_code: str) -> list[DeviceInfo]:
        return [d for d in self._by_code.values() if d.room_code == room_code]

    def high_risk(self) -> list[DeviceInfo]:
        """高风险设备清单（门锁/燃气阀/总闸）—— 只用于生成待确认建议。"""
        return [d for d in self._by_code.values() if d.high_risk]

    @property
    def rooms(self) -> list[RoomInfo]:
        return list(self._rooms)

    def room(self, code: str) -> RoomInfo | None:
        return self._rooms_by_code.get(code)

    def rooms_of(self, home_id: int) -> list[RoomInfo]:
        return [r for r in self._rooms if r.home_id == home_id]

    def home_ids(self) -> list[int]:
        return sorted({r.home_id for r in self._rooms})

    def __len__(self) -> int:
        return len(self._by_code)
