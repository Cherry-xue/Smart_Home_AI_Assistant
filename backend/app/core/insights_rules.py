"""安防 / 照护 / 建议的规则配置加载。

配置来自 `config/insights_rules.yaml`（项目级共享文件，README 里解释过为什么阈值
要外置而不是写死在代码里）。

**这个模块只做三件事**：读 yaml、把字段转成带类型的对象、把时间字符串转成 `time`。
所有"判定逻辑"都在 `services/` 里 —— 配置与代码分离，改阈值不用改逻辑。

⚠️ 一个刻意的设计：**规则对象是不可变的（frozen）**。
判定过程中的任何"累积"都放在调用方的局部变量里，避免出现
"跑了一次告警推导，规则里的阈值被悄悄改了"这种极难查的问题。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
from pathlib import Path
from typing import Any

import yaml

# backend/app/core/insights_rules.py -> 上溯 3 层为项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
RULES_PATH = PROJECT_ROOT / "config" / "insights_rules.yaml"

VALID_LEVELS = ("info", "warning", "critical")
VALID_DISTURBANCE = ("urgent", "important", "normal", "silent")


def _hhmm(s: str) -> dt.time:
    h, m = str(s).split(":")
    return dt.time(int(h), int(m))


@dataclasses.dataclass(frozen=True)
class EventRule:
    """传感器事件 → 告警的映射。"""

    event_code: str
    alarm_code: str
    level: str
    title: str
    criteria: str
    action_hint: str
    suggestion_level: str


@dataclasses.dataclass(frozen=True)
class StateRule:
    """设备状态 → 告警的规则。`params` 是各规则自己的阈值（分钟数、设备类型等）。"""

    name: str
    enabled: bool
    level: str
    title: str
    criteria: str
    action_hint: str
    params: dict[str, Any]

    def num(self, key: str, default: float = 0.0) -> float:
        v = self.params.get(key, default)
        return float(v) if v is not None else default

    def strs(self, key: str) -> list[str]:
        v = self.params.get(key) or []
        return [str(x) for x in v]


@dataclasses.dataclass(frozen=True)
class CareSubject:
    """照护对象与感知设备的绑定（配置项，不是自动推断）。"""

    member_nickname: str
    room_code: str
    presence_device: str
    door_device: str | None = None
    note: str = ""


@dataclasses.dataclass(frozen=True)
class CareThresholds:
    no_activity_hours: float
    bucket_minutes: float
    active_activity_level: float
    bed_minutes: float
    segment_gap_minutes: float
    min_samples_per_segment: int
    night_start: dt.time
    night_end: dt.time
    morning_start: dt.time
    morning_end: dt.time
    evening_start: dt.time
    evening_end: dt.time
    window_hours: float

    @staticmethod
    def _in(t: dt.time, start: dt.time, end: dt.time) -> bool:
        """是否落在时段内，支持跨零点。"""
        if start <= end:
            return start <= t < end
        return t >= start or t < end

    def is_night(self, t: dt.time) -> bool:
        return self._in(t, self.night_start, self.night_end)

    def is_morning(self, t: dt.time) -> bool:
        return self._in(t, self.morning_start, self.morning_end)

    def is_evening(self, t: dt.time) -> bool:
        return self._in(t, self.evening_start, self.evening_end)


@dataclasses.dataclass(frozen=True)
class SuggestionRule:
    name: str
    enabled: bool
    level: str
    params: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class InsightsRules:
    event_rules: dict[str, EventRule]
    state_rules: dict[str, StateRule]
    subjects: tuple[CareSubject, ...]
    care: CareThresholds
    suggestion_rules: dict[str, SuggestionRule]
    suggestion_visible_status: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str = RULES_PATH) -> InsightsRules:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

        event_rules: dict[str, EventRule] = {}
        for code, cfg in (raw.get("security", {}).get("event_rules") or {}).items():
            level = str(cfg.get("level", "warning")).lower()
            sugg = str(cfg.get("suggestion_level", "normal")).lower()
            event_rules[str(code)] = EventRule(
                event_code=str(code),
                alarm_code=str(cfg["alarm_code"]),
                level=level if level in VALID_LEVELS else "warning",
                title=str(cfg.get("title") or code),
                criteria=str(cfg.get("criteria") or ""),
                action_hint=str(cfg.get("action_hint") or ""),
                suggestion_level=sugg if sugg in VALID_DISTURBANCE else "normal",
            )

        state_rules: dict[str, StateRule] = {}
        for name, cfg in (raw.get("security", {}).get("state_rules") or {}).items():
            level = str(cfg.get("level", "warning")).lower()
            params = {k: v for k, v in cfg.items()
                      if k not in ("enabled", "level", "title", "criteria", "action_hint")}
            state_rules[str(name)] = StateRule(
                name=str(name),
                enabled=bool(cfg.get("enabled", True)),
                level=level if level in VALID_LEVELS else "warning",
                title=str(cfg.get("title") or name),
                criteria=str(cfg.get("criteria") or ""),
                action_hint=str(cfg.get("action_hint") or ""),
                params=params,
            )

        care_raw = raw.get("care") or {}
        th = care_raw.get("thresholds") or {}
        care = CareThresholds(
            no_activity_hours=float(th.get("no_activity_hours", 4)),
            bucket_minutes=float(th.get("bucket_minutes", 5)),
            active_activity_level=float(th.get("active_activity_level", 20)),
            bed_minutes=float(th.get("bed_minutes", 60)),
            segment_gap_minutes=float(th.get("segment_gap_minutes", 20)),
            min_samples_per_segment=int(th.get("min_samples_per_segment", 2)),
            night_start=_hhmm(th.get("night_start", "22:00")),
            night_end=_hhmm(th.get("night_end", "06:00")),
            morning_start=_hhmm(th.get("morning_start", "05:00")),
            morning_end=_hhmm(th.get("morning_end", "09:30")),
            evening_start=_hhmm(th.get("evening_start", "21:00")),
            evening_end=_hhmm(th.get("evening_end", "01:00")),
            window_hours=float(care_raw.get("window_hours", 24)),
        )
        subjects = tuple(
            CareSubject(
                member_nickname=str(s["member_nickname"]),
                room_code=str(s["room_code"]),
                presence_device=str(s["presence_device"]),
                door_device=(str(s["door_device"]) if s.get("door_device") else None),
                note=str(s.get("note") or ""),
            )
            for s in (care_raw.get("subjects") or [])
        )

        sugg_raw = raw.get("suggestions") or {}
        suggestion_rules = {
            str(name): SuggestionRule(
                name=str(name),
                enabled=bool(cfg.get("enabled", True)),
                level=str(cfg.get("level", "normal")).lower(),
                # 模板也一起收进来：文案属于展示层配置，写在 yaml 里改文案不用改代码
                params={k: v for k, v in cfg.items() if k not in ("enabled", "level")},
            )
            for name, cfg in (sugg_raw.get("rules") or {}).items()
        }

        return cls(
            event_rules=event_rules,
            state_rules=state_rules,
            subjects=subjects,
            care=care,
            suggestion_rules=suggestion_rules,
            suggestion_visible_status=tuple(
                str(x) for x in (sugg_raw.get("visible_status") or ["pending", "shown", "accepted"])
            ),
        )


@functools.lru_cache(maxsize=1)
def get_insights_rules() -> InsightsRules:
    """进程内单例（yaml 很小，但没必要每次请求都读盘）。"""
    return InsightsRules.load()
