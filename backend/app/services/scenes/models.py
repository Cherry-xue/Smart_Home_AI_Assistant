"""场景编排的数据结构。

一个刻意的选择：**场景步骤直接绑定"能力"而不是"语义动作"**
----------------------------------------------------------
即：不用 `{"action": "调暗", "target": "客厅灯"}` 这种中间层，
而是直接 `{"device_code": "living_room_light_main", "capability": "brightness", "value": 20}`。

理由：
1. 能力档案（`device_capabilities`）已经是"设备能做什么"的唯一事实来源，
   再发明一层语义动词只会多一次翻译、多一处不一致；
2. 校验变得直接：有 `capability` 就对得上档案，有 `value` 就查得到范围；
3. LLM 的提示词里本来就会带上能力档案，让它直接填能力名反而更准。
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass
class SceneStep:
    """一条候选动作（来自 LLM 或人工编排）。"""

    device_code: str
    capability: str
    value: Any

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SceneStep:
        return cls(
            device_code=str(raw.get("device_code") or raw.get("device") or "").strip(),
            capability=str(raw.get("capability") or raw.get("code") or "").strip(),
            value=raw.get("value"),
        )


@dataclasses.dataclass
class StepOutcome:
    """一条动作经过校验/执行之后的结果。

    `status` 的取值与含义
    --------------------
    | status | 含义 |
    |---|---|
    | `ok` | 通过校验，准备执行 |
    | `warn` | 通过，但有提示（如意图规则只是提醒） |
    | `clamped` | 值被夹到能力范围内或意图区间内（`original_value` 记录原值） |
    | `rejected` | **被剔除**（设备不存在 / 无此能力 / 不可写 / 违反意图） |
    | `duplicate` | 因重复被"后写覆盖"剔除 |
    | `pending_confirm` | 高风险动作，**只生成待确认建议**，不执行 |
    | `executed` | 已下发 |
    | `failed` | 下发失败（重试后仍失败） |
    """

    device_code: str
    capability: str
    value: Any
    device_id: int | None = None
    device_name: str = ""
    room_name: str = ""
    original_value: Any = None
    status: str = "ok"
    reason: str = ""
    risk_level: str = "low"
    high_risk_device: bool = False
    # 执行阶段回填
    command_id: int | None = None
    attempts: int = 0
    error: str | None = None

    @property
    def is_rejected(self) -> bool:
        return self.status in ("rejected", "duplicate")

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ScenePlan:
    """编排结果：能不能做、做了什么改动、剔除了什么、为什么。"""

    intent_key: str
    intent_label: str
    raw_count: int
    actions: list[StepOutcome] = dataclasses.field(default_factory=list)
    rejected: list[StepOutcome] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def pending_confirm_count(self) -> int:
        return sum(1 for a in self.actions if a.high_risk_device)

    @property
    def executable_count(self) -> int:
        return sum(1 for a in self.actions if not a.high_risk_device)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_key": self.intent_key,
            "intent_label": self.intent_label,
            "raw_count": self.raw_count,
            "action_count": len(self.actions),
            "rejected_count": len(self.rejected),
            "executable_count": self.executable_count,
            "pending_confirm_count": self.pending_confirm_count,
            "actions": [a.as_dict() for a in self.actions],
            "rejected": [a.as_dict() for a in self.rejected],
            "notes": self.notes,
        }


@dataclasses.dataclass
class RunResult:
    """一次场景执行的结果。"""

    status: str            # success / partial / failed
    started_at: Any
    finished_at: Any = None
    scene_id: int | None = None
    run_id: int | None = None
    executed: list[StepOutcome] = dataclasses.field(default_factory=list)
    pending_confirm: list[StepOutcome] = dataclasses.field(default_factory=list)
    failed: list[StepOutcome] = dataclasses.field(default_factory=list)
    fallback_used: bool = False
    notes: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scene_id": self.scene_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "executed_count": len(self.executed),
            "pending_confirm_count": len(self.pending_confirm),
            "failed_count": len(self.failed),
            "fallback_used": self.fallback_used,
            "executed": [s.as_dict() for s in self.executed],
            "pending_confirm": [s.as_dict() for s in self.pending_confirm],
            "failed": [s.as_dict() for s in self.failed],
            "notes": self.notes,
        }
