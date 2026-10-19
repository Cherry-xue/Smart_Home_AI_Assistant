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
    home_id: int | None = Field(
        None,
        description=(
            "在哪一套房里编排。给值就只在这套房的设备/房间里挑；"
            "不传 = 不过滤（兼容旧调用方与回归脚本，多套房下**必须传**）"
        ),
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
    home_id: int | None = Field(
        None,
        description="在哪一套房里校验。不传 = 不过滤（多套房下必须传，否则会挑到别套房的设备）",
    )


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
    favorite: bool = Field(
        False, description="是否收藏（总览页只显示收藏的场景）。存在 definition_json 里，不改表"
    )
    actions: list[SceneStepIn] = Field(
        default_factory=list, description="已保存的动作，编辑弹窗直接用它，省一次详情请求"
    )
    created_at: dt.datetime | None = None


class SceneUpdateIn(BaseModel):
    """编辑场景。字段都可选：只传要改的。

    ⚠️ `actions` **不传** = 动作保持不变；传**空数组** = 想清空动作 → 会被拒（422），
    因为"没有动作的场景"执行时只会报错。
    """

    name: str | None = Field(None, min_length=1, max_length=64)
    description: str | None = None
    actions: list[SceneStepIn] | None = None
    enabled: bool | None = None
    favorite: bool | None = None


class SceneFavoriteIn(BaseModel):
    favorite: bool = True


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
