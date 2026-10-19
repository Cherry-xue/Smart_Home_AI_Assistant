"""API 数据模型：安防告警 / 照护观察 / 主动建议。

这三个模块共用一条原则：**结论必须带依据**。
所以每条记录除了"是什么"，还带着"凭什么这么说"：
- 告警带 `detail`（判据 + 证据 + 建议处置，纯文本多行）；
- 照护带 `confidence` + `remark` + 整体 `coverage`（数据够不够）；
- 建议带 `reason`（规则名、阈值、命中的具体数值）。

前端因此可以老实回答用户的追问，而不是只能说"系统认为"。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


# ============================================================================
# 安防告警
# ============================================================================
class AlarmOut(BaseModel):
    id: int
    alarm_code: str
    level: str = Field(..., description="info / warning / critical")
    title: str
    detail: str | None = Field(None, description="判据 + 证据 + 建议处置（纯文本，含换行）")
    status: str = Field(..., description="active / acknowledged / resolved / dismissed")
    is_active: bool = Field(False, description="active 或 acknowledged")

    room_id: int | None = None
    room_code: str | None = None
    room_name: str | None = None
    device_id: int | None = None
    device_code: str | None = None
    device_name: str | None = None
    device_type: str | None = None

    created_at: dt.datetime
    age_minutes: float | None = Field(None, description="相对数据锚点的分钟数")
    acknowledged_by: int | None = None
    acknowledged_at: dt.datetime | None = None


class AlarmSyncOut(BaseModel):
    """这次请求做了哪些推导 —— 让"为什么现在有这条告警"可追溯。"""

    event_created: int = 0
    event_skipped: int = 0
    state_created: int = 0
    state_resolved: int = 0
    events_scanned: int = 0
    events_unscored: int = Field(0, description="没配规则、只入库不告警的事件数")


class AlarmListOut(BaseModel):
    items: list[AlarmOut] = Field(default_factory=list)
    total: int = 0
    by_level: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    sync: AlarmSyncOut = Field(default_factory=AlarmSyncOut)
    # （已移除 disclaimer）告警详情里那句「阈值为演示值」是废话，界面上不显示，
    # 响应里也不再带。阈值出处查 config/insights_rules.yaml。


class AlarmAckIn(BaseModel):
    acknowledged_by: int | None = Field(
        None, description="确认人（members.id）。留空表示经系统界面确认，未指定具体成员"
    )


class AlarmAckOut(BaseModel):
    id: int
    status: str
    acknowledged_at: dt.datetime | None = None
    message: str = Field("", description="给用户看的提示：确认 ≠ 已执行")


# ============================================================================
# 照护观察
# ============================================================================
class ObservationOut(BaseModel):
    id: int
    member_id: int
    nickname: str
    role_code: str
    birth_year: int | None = None

    observed_at: dt.datetime
    activity_type: str
    activity_label: str
    duration_min: int | None = None
    source_device_id: int | None = None
    source_device_code: str | None = None
    source_device_name: str | None = None
    confidence: float | None = Field(None, description="数据可信度（采样完整度），不是健康风险")
    remark: str | None = None
    is_clue: bool = Field(False, description="true 表示需要人工关注的待确认线索")


class MemberCareSummaryOut(BaseModel):
    member_id: int
    nickname: str
    role_code: str
    observations: int = 0
    clues: int = 0
    last_activity_at: dt.datetime | None = None
    has_inactivity_clue: bool = False


class CoverageWindowOut(BaseModel):
    from_: dt.datetime = Field(..., alias="from")
    to: dt.datetime

    model_config = {"populate_by_name": True}


class SubjectCoverageOut(BaseModel):
    member_id: int
    nickname: str
    device_code: str
    window: CoverageWindowOut
    buckets_expected: int
    buckets_actual: int
    buckets_with_presence: int
    coverage_ratio: float
    sufficient: bool = Field(..., description="false 时不得据此推断活动节律")


class ObservationListOut(BaseModel):
    items: list[ObservationOut] = Field(default_factory=list)
    total: int = 0
    members: list[MemberCareSummaryOut] = Field(default_factory=list)
    coverage: list[SubjectCoverageOut] = Field(
        default_factory=list, description="每个照护对象这段数据够不够看，先看这里再看结论"
    )
    # （已移除 disclaimer）"不做医疗断言"由文案红线保证，不再单挂免责句。


# ============================================================================
# 主动建议
# ============================================================================
class SuggestionOut(BaseModel):
    id: int
    member_id: int | None = None
    nickname: str | None = None
    content: str
    reason: dict[str, Any] = Field(default_factory=dict, description="规则名、阈值、命中数值")
    disturbance_level: str = Field(..., description="urgent / important / normal / silent")
    status: str = Field(..., description="pending / shown / accepted / dismissed")
    is_open: bool = False
    quiet_downgraded: bool = Field(False, description="是否因免打扰时段被降级为 silent")
    created_at: dt.datetime
    dismissed_at: dt.datetime | None = None


class SuggestionListOut(BaseModel):
    items: list[SuggestionOut] = Field(default_factory=list)
    total: int = 0
    by_level: dict[str, int] = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list, description="等级排列顺序（高→低）")
    disclaimer: str = ""


class SuggestionActionOut(BaseModel):
    id: int
    status: str
    message: str = ""
