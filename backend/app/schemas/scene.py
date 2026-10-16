"""场景编排 API 的数据模型。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class SceneStepIn(BaseModel):
    """一条候选动作（前端手动微调时也用这个结构）。"""

    device_code: str = Field(..., description="设备编码，必须来自设备目录")
    capability: str = Field(..., description="能力编码，必须登记在能力档案里")
    value: Any = Field(None, description="目标值；类型与范围由能力档案决定")


class SceneActionOut(BaseModel):
    """一条动作经过校验/执行后的结果（这是"能复现被剔除原因"的载体）。"""

    device_code: str
    device_name: str = ""
    room_name: str = ""
    capability: str
    value: Any = None
    original_value: Any = Field(None, description="被夹取前的原始值（status=clamped 时有意义）")
    status: str = Field(
        ...,
        description=(
            "ok / warn / clamped / **rejected**（被剔除）/ **duplicate**（被覆盖）/ "
            "pending_confirm（高风险，仅生成建议）/ executed / failed"
        ),
    )
    reason: str = Field("", description="为什么是现在这个状态 —— 剔除、夹取、待确认的原因都在这里")
    risk_level: str = "low"
    high_risk_device: bool = False
    command_id: int | None = None
    attempts: int = 0
    error: str | None = None


class ScenePreviewIn(BaseModel):
    query: str = Field(..., min_length=1, description="自然语言，如『把客厅调成看电影的样子』")
    dry_run_execute: bool = Field(
        False, description="是否顺带演练一遍执行流程（不真下发、不写库）"
    )


class ScenePreviewOut(BaseModel):
    query: str
    understanding: str = Field("", description="模型对这句话的理解（便于用户确认它有没有理解错）")
    intent_key: str
    intent_label: str
    raw_count: int = Field(..., description="模型给出的候选动作数")
    action_count: int
    rejected_count: int
    executable_count: int
    pending_confirm_count: int
    actions: list[SceneActionOut] = Field(default_factory=list)
    rejected: list[SceneActionOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    model_output: str | None = Field(None, description="模型原始输出（排障用）")


class ScenePlanIn(BaseModel):
    """对手动调整过的动作列表重新编排（前端"微调后看冲突"用）。"""

    actions: list[SceneStepIn]
    scene_name: str = ""
    description: str = ""


class ScenePlanOut(BaseModel):
    intent_key: str
    intent_label: str
    raw_count: int
    action_count: int
    rejected_count: int
    executable_count: int
    pending_confirm_count: int
    actions: list[SceneActionOut] = Field(default_factory=list)
    rejected: list[SceneActionOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SceneCreateIn(BaseModel):
    home_id: int
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    actions: list[SceneStepIn]
    trigger_type: str = Field("manual", description="manual / schedule / sensor / voice")
    enabled: bool = True


class SceneOut(BaseModel):
    id: int
    home_id: int
    name: str
    description: str = ""
    trigger_type: str
    action_count: int
    intent_key: str | None = None
    enabled: bool
    created_at: dt.datetime | None = None


class SceneRunOut(BaseModel):
    status: str = Field(..., description="success / partial（降级执行）/ failed")
    scene_id: int | None = None
    run_id: int | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    executed_count: int = 0
    pending_confirm_count: int = 0
    failed_count: int = 0
    fallback_used: bool = Field(False, description="是否有步骤失败但仍继续执行了其余动作")
    executed: list[SceneActionOut] = Field(default_factory=list)
    pending_confirm: list[SceneActionOut] = Field(default_factory=list)
    failed: list[SceneActionOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
