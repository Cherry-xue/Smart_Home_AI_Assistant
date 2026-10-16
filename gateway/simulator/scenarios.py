"""预置场景：制造"家里出事了"的时刻。

为什么需要它
------------
安防、照护两条业务线**只在异常发生时才有戏**。如果只会跑正常数据，
那"燃气泄漏 → 分级告警 → 处置时间轴"这条演示链路永远触发不了。

每个场景做两件事：
1. **覆盖传感器读数**（让数据层面真的出现异常值）
2. **补发一条 event 消息**（安防/照护的事件入口）

⚠️ 生成器只制造"观测到异常"，**绝不产生任何控制指令** ——
燃气阀关阀、门锁开锁这类动作永远只能由后端生成"待确认建议"。
"""

from __future__ import annotations

import dataclasses
import random

try:  # 同时支持两种运行方式
    from .physics import Sample
    from .registry import DeviceSpec
except ImportError:
    from physics import Sample
    from registry import DeviceSpec


@dataclasses.dataclass
class ScenarioStep:
    label: str
    at_min: float                 # 模拟开始后第几分钟触发
    duration_min: float
    # 覆盖哪些设备的哪些指标（按 device_type 或 device_code 匹配）
    overrides: dict[str, dict[str, object]]
    event: dict[str, object] | None = None


@dataclasses.dataclass
class ActiveOverride:
    overrides: dict[str, dict[str, object]]
    event: dict[str, object] | None


SCENARIOS: dict[str, ScenarioStep] = {
    "gas_leak": ScenarioStep(
        label="厨房燃气泄漏",
        at_min=120, duration_min=4,
        overrides={
            "gas_sensor": {"gas_concentration": 42.0},
            "air_sensor": {"pm25": 95.0},
        },
        event={
            "event_code": "gas_leak", "level": "critical",
            "title": "厨房检测到燃气泄漏",
            "detail": "燃气浓度 42 %LEL，已超过 25 %LEL 阈值。仅为单传感器读数，需交叉核实。",
            "suggested_action": "关闭燃气阀（需人工确认）+ 开窗通风",
        },
    ),
    "water_leak": ScenarioStep(
        label="厨房水浸",
        at_min=240, duration_min=6,
        overrides={"water_leak_sensor": {"water_detected": True}},
        event={
            "event_code": "water_leak", "level": "warning",
            "title": "厨房检测到水浸",
            "detail": "水浸传感器触发。建议检查水管与洗碗机。",
            "suggested_action": "通知住户核实",
        },
    ),
    "smoke": ScenarioStep(
        label="厨房烟雾",
        at_min=480, duration_min=3,
        overrides={
            "smoke_sensor": {"smoke_detected": True},
            "air_sensor": {"pm25": 180.0, "co2": 1400.0},
        },
        event={
            "event_code": "smoke", "level": "critical",
            "title": "厨房检测到烟雾",
            "detail": "烟感触发，PM2.5 同步升至 180 μg/m³。需排除烹饪油烟误报。",
            "suggested_action": "核实是否明火，必要时联系物业",
        },
    ),
    "no_activity": ScenarioStep(
        label="次卧长时间无活动（照护）",
        at_min=300, duration_min=360,
        overrides={
            "mmwave_radar": {"presence": False, "activity_level": 0},
            "door_sensor": {"open": False},
        },
        event={
            "event_code": "care_no_activity", "level": "warning",
            "title": "次卧连续 6 小时无活动迹象",
            "detail": "毫米波雷达 6 小时内未检测到活动，门磁无开合记录。**这是待确认线索，不是健康结论。**",
            "suggested_action": "按家庭预案联系成员核实，不自动拨打紧急电话",
        },
    ),
    "night_wake": ScenarioStep(
        label="老人夜间起夜",
        at_min=180, duration_min=20,
        overrides={"mmwave_radar": {"presence": True, "activity_level": 72}},
        event=None,
    ),
    "intrusion": ScenarioStep(
        label="阳台窗户异常开启",
        at_min=60, duration_min=10,
        overrides={"door_sensor": {"open": True}},
        event={
            "event_code": "intrusion", "level": "warning",
            "title": "检测到门窗异常开启",
            "detail": "门磁在非出入时段被触发。仅为单点信号，需结合其他传感器交叉验证。",
            "suggested_action": "核实是否住户操作，避免直接升级为入侵告警",
        },
    ),
}


class ScenarioRunner:
    """按模拟时间驱动场景；一次只跑一个场景（可用 --scenario random 让它随机挑）。"""

    def __init__(self, name: str, rng: random.Random, auto_repeat: bool = False):
        self.rng = rng
        self.name = name
        if name == "none":
            self.steps: list[ScenarioStep] = []
        elif name == "random":
            self.steps = [SCENARIOS[self.rng.choice(list(SCENARIOS))]]
            self.auto_repeat = auto_repeat
        else:
            if name not in SCENARIOS:
                raise SystemExit(f"未知场景 {name!r}，可选：none / random / {', '.join(SCENARIOS)}")
            self.steps = [SCENARIOS[name]]
            self.auto_repeat = auto_repeat
        self._fired: set[str] = set()

    def active(self, elapsed_min: float) -> ActiveOverride | None:
        for step in self.steps:
            if step.at_min <= elapsed_min < step.at_min + step.duration_min:
                first = step.label not in self._fired
                self._fired.add(step.label)
                return ActiveOverride(
                    overrides=step.overrides,
                    event=step.event if first else None,   # 事件只在进入窗口时发一次
                )
        return None

    @property
    def steps_summary(self) -> str:
        if not self.steps:
            return "无场景（只产正常数据）"
        return "; ".join(
            f"{s.label} @第{int(s.at_min)}分钟起 {int(s.duration_min)}分钟" for s in self.steps
        )


def apply_overrides(samples: list[Sample], ov: ActiveOverride | None) -> list[Sample]:
    """把场景的覆盖值写到样本上。"""
    if ov is None:
        return samples
    out: list[Sample] = []
    for s in samples:
        override = ov.overrides.get(s.device.device_type) or ov.overrides.get(s.device.code)
        if not override:
            out.append(s)
            continue
        metrics = dict(s.metrics)
        metrics.update(override)
        # 场景值同样要过一遍能力档案钳制（不改的话可能自相矛盾）
        for k, v in override.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                metrics[k] = s.device.clamp(k, v)
        out.append(Sample(s.device, s.kind, metrics, dict(s.units)))
    return out


def build_event_message(
    ov: ActiveOverride, devices: list[DeviceSpec]
) -> tuple[DeviceSpec, dict] | None:
    """把场景事件挂到一个合适的设备上（按事件类型挑设备）。"""
    if not ov or not ov.event:
        return None
    ev = dict(ov.event)
    code = ev.get("event_code", "")
    want_type = {
        "gas_leak": "gas_sensor",
        "water_leak": "water_leak_sensor",
        "smoke": "smoke_sensor",
        "care_no_activity": "mmwave_radar",
        "intrusion": "door_sensor",
    }.get(code)
    dev = next((d for d in devices if d.device_type == want_type), None)
    if dev is None:
        return None
    return dev, ev
