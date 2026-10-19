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
    """按模拟时间驱动场景。

    两种模式
    --------
    - **单次**（默认）：只跑一个场景，在它自己声明的 `at_min` 触发一次。
      适合复现/调试（`--scenario gas_leak`）。
    - **循环**（`auto_repeat=True`）：按一张"随机时间表"反复触发，
      两次之间留 `gap` 分钟的空窗。

    为什么需要循环模式
    ------------------
    安防/照护两条业务线**只在异常发生时才有数据**。单次模式下，常驻生成器
    跑过那个时间点之后就再也不会有事件了 —— 库里 `sensor_events` 只有启动初期
    那几条，告警页看上去永远空着。这不是"逻辑坏了"，是**数据源不会再来**。

    ⚠️ 用"发牌"而不是"每次独立随机"来选场景：独立随机会连续抽到同一个场景
    （燃气泄漏一晚上演三遍），发牌保证一轮之内不重复。
    """

    #: 循环模式下首场异常最早/最晚在模拟开始后多少分钟出现
    FIRST_AT = (10.0, 40.0)
    #: 之后每两场之间的空窗（模拟分钟）
    GAP = (150.0, 330.0)

    def __init__(
        self,
        name: str,
        rng: random.Random,
        auto_repeat: bool = False,
    ):
        self.rng = rng
        self.name = name
        self.auto_repeat = auto_repeat

        if name == "none":
            self._pool: list[str] = []
        elif name == "random":
            self._pool = sorted(SCENARIOS)          # 排序后再洗牌，保证同 seed 可复现
        else:
            if name not in SCENARIOS:
                raise SystemExit(f"未知场景 {name!r}，可选：none / random / {', '.join(SCENARIOS)}")
            self._pool = [name]

        self._deck: list[str] = []
        #: 已排定的场次：(序号, 具体时刻的 step)
        self._occurrences: list[tuple[int, ScenarioStep]] = []
        self._occ_seq = 0
        self._fired: set[int] = set()

        if not self._pool:
            self._next_at = float("inf")
        elif auto_repeat:
            self._next_at = self.rng.uniform(*self.FIRST_AT)
        else:
            # 单次模式：沿用场景自己声明的 at_min，行为与改造前完全一致
            self._schedule_at(SCENARIOS[self._pool[0]].at_min)
            self._next_at = float("inf")

    # ------------------------------------------------------------------ 内部
    def _draw(self) -> str:
        """发牌：洗好的一副牌发完再洗，保证一轮内不重复。"""
        if not self._deck:
            self._deck = list(self._pool)
            self.rng.shuffle(self._deck)
        return self._deck.pop()

    def _schedule_at(self, at_min: float) -> None:
        self._occ_seq += 1
        self._occurrences.append((self._occ_seq, dataclasses.replace(SCENARIOS[self._draw()], at_min=at_min)))
        # 只保留最近两场，避免长跑时列表无限增长
        if len(self._occurrences) > 2:
            self._occurrences.pop(0)

    # ------------------------------------------------------------------ 对外
    def trigger(self, name: str, at_min: float, duration_min: float | None = None) -> bool:
        """**手动**触发一场场景（演示/联调用）。

        为什么要它：常驻生成器追上真实时间后会降到 1 倍速，而自动场景的间隔是
        2.5~5.5 小时 —— 现场演示不可能等这么久。所以留一个"现在就来一次"的入口，
        通过 MQTT 控制主题触发（见 `scripts/trigger_scenario.py`）。

        ⚠️ 手动触发同样**只制造观测**，不产生任何设备指令。
        """
        if name not in SCENARIOS:
            return False
        step = SCENARIOS[name]
        if duration_min:
            step = dataclasses.replace(step, duration_min=float(duration_min))
        self._occ_seq += 1
        self._occurrences.append((self._occ_seq, dataclasses.replace(step, at_min=at_min)))
        if len(self._occurrences) > 3:
            self._occurrences.pop(0)
        return True

    def active(self, elapsed_min: float) -> ActiveOverride | None:
        if not self._pool and not self._occurrences:
            return None

        # 到点了就排下一场（模拟时间只增不减，所以这里不会重复排同一场）
        if self.auto_repeat and elapsed_min >= self._next_at:
            self._schedule_at(self._next_at)
            self._next_at = self._next_at + self.rng.uniform(*self.GAP)

        for occ_id, step in reversed(self._occurrences):
            if step.at_min <= elapsed_min < step.at_min + step.duration_min:
                first = occ_id not in self._fired
                self._fired.add(occ_id)
                return ActiveOverride(
                    overrides=step.overrides,
                    event=step.event if first else None,   # 事件只在进入窗口时发一次
                )
        return None

    @property
    def steps_summary(self) -> str:
        if not self._pool:
            return "无场景（只产正常数据）"
        if self.auto_repeat:
            return (
                f"循环模式：从第 {self.FIRST_AT[0]:.0f}~{self.FIRST_AT[1]:.0f} 分钟起，"
                f"约每 {self.GAP[0]:.0f}~{self.GAP[1]:.0f} 分钟一场；"
                f"候选 {len(self._pool)} 个场景（发牌不重复）：{', '.join(self._pool)}"
            )
        return "; ".join(
            f"{s.label} @第{int(s.at_min)}分钟起 {int(s.duration_min)}分钟" for _, s in self._occurrences
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
