"""API 数据模型：通用部分（设备、房间、能力）。

为什么坚持给每个接口写 response_model
------------------------------------
1. FastAPI 会自动生成 OpenAPI 文档，前端**不需要靠猜**来对接（规划书 §10.3 要求契约先行）；
2. 响应结构一旦被模型固定，就不会因为内部查询改动而悄悄变字段名；
3. 所有值都带 `observed_at / quality / unit` 元数据 —— 这是规划书对接口的硬要求，
   前端才能回答"这个数字是几点测的、可不可信"。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 元数据
class LatestMetricOut(BaseModel):
    """一个指标的最新值。**没有值就是 null，不会用 0 冒充。**"""

    value: Any = Field(None, description="指标值（bool/数值/字符串，取决于能力档案的 value_type）")
    unit: str | None = None
    observed_at: dt.datetime | None = Field(None, description="观测时间（本地时区）")
    quality: str = Field("good", description="good / suspect / bad")
    seq: int | None = None


class CapabilityOut(BaseModel):
    code: str
    display_name: str
    value_type: str = Field(..., description="bool / int / float / enum / string")
    unit: str | None = None
    value_min: float | None = None
    value_max: float | None = None
    enum_values: list[str] = Field(default_factory=list)
    writable: bool = Field(False, description="是否可写（传感器恒为 false）")
    risk_level: str = Field("low", description="low / medium / high —— 该能力参与的动作有多危险")


class KeyMetricOut(BaseModel):
    metric_code: str
    value: Any = None
    unit: str | None = None
    device_id: int
    device_name: str
    observed_at: dt.datetime | None = None
    quality: str = "good"


# ---------------------------------------------------------------- 房间
class RoomCardOut(BaseModel):
    room_id: int
    code: str
    name: str
    sort_no: int
    device_count: int
    online_count: int
    offline_count: int
    high_risk_count: int
    active_alarm_count: int = Field(0, description="alarms 表中 status=active 的条数")
    recent_event_count: int = Field(0, description="时间窗内的 sensor_events 条数")
    last_update_at: dt.datetime | None = Field(None, description="该房间最后一次数据更新")
    key_metrics: list[KeyMetricOut] = Field(default_factory=list)


class RoomsOut(BaseModel):
    home_id: int
    generated_at: dt.datetime
    items: list[RoomCardOut]


class HomeOut(BaseModel):
    """一套房（顶部切换用）。"""

    id: int
    name: str
    timezone: str | None = None
    rooms: int = Field(0, description="房间数")
    devices: int = Field(0, description="启用中的设备数")
    online: int = Field(0, description="在线设备数")


class HomesOut(BaseModel):
    items: list[HomeOut] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------- 设备
class DeviceOut(BaseModel):
    id: int
    code: str
    name: str
    device_type: str
    room_code: str
    room_name: str
    high_risk: bool = Field(..., description="门锁/燃气阀/总闸为 true")
    auto_exec_allowed: bool = Field(
        ..., description="是否允许自动执行控制指令。高风险设备恒为 false（项目安全红线）"
    )
    online: bool
    last_seen: dt.datetime | None = Field(
        None, description="数据源最后一次实时上报的时间（在线判定依据）"
    )
    last_observed_at: dt.datetime | None = Field(
        None, description="手上最新数据的观测时间（含启动预热），前端显示『数据更新时间』用这个"
    )
    data_age_sec: float | None = None
    capability_count: int
    writable_count: int
    latest: dict[str, LatestMetricOut] = Field(default_factory=dict)


class DeviceListMeta(BaseModel):
    total: int = Field(..., description="设备总数")
    filtered: int = Field(..., description="过滤后的条数")
    summary: dict[str, int] = Field(default_factory=dict, description="全屋统计")
    generated_at: dt.datetime


class DeviceListOut(BaseModel):
    meta: DeviceListMeta
    items: list[DeviceOut]


class DeviceCapabilitiesOut(BaseModel):
    device_id: int
    code: str
    name: str
    device_type: str
    room_code: str
    room_name: str
    high_risk: bool
    auto_exec_allowed: bool
    capabilities: list[CapabilityOut] = Field(default_factory=list)


# ---------------------------------------------------------------- 历史读数
class ReadingPointOut(BaseModel):
    observed_at: dt.datetime
    value: Any = None
    quality: str = "good"
    is_stale: bool = False
    seq: int | None = None


class MetricSeriesOut(BaseModel):
    metric_code: str
    unit: str | None = None
    count: int
    points: list[ReadingPointOut] = Field(default_factory=list)


class DeviceReadingsOut(BaseModel):
    device_id: int
    device_code: str
    device_name: str
    room_code: str
    high_risk: bool
    from_: dt.datetime = Field(..., alias="from")
    to: dt.datetime
    #: **原始行数**（库里真实有多少条），不是返回的点数
    row_count: int
    #: 实际返回的点数。row_count 大而 returned_count 小时，说明被降采样了
    returned_count: int = 0
    #: 是否降采样过。前端应当据此提示"曲线是抽稀的，别用它读精确极值"
    downsampled: bool = False
    #: 抽稀步长（每 stride 行取 1 点）；未抽稀时为 1
    stride: int = 1
    stale_rows: int
    quality_summary: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, MetricSeriesOut] = Field(default_factory=dict)
    note: str | None = None

    model_config = {"populate_by_name": True}


# ============================================================ 套房的增删
#: 新建套房的默认房间：**一个都不建**。
#:
#: 为什么不给占位房间：房间的"名字"和"位置"是同一件事，都在户型编辑器里定。
#: 自动生成的占位会变成"图上没有但列表里有"的幽灵房间，只能靠「移除」清掉 ——
#: 与其让用户清理，不如一开始就不造。
DEFAULT_ROOM_NAMES: tuple[str, ...] = ()


class HomeCreateIn(BaseModel):
    """新建套房。

    `rooms` 传的是**房间显示名**（用户可以起中文名），房间编码由后端生成。
    为什么不收编码：编码要进 MQTT topic，带 `/ + #` 会破坏主题结构，
    让用户填等于给协议留后门。
    """

    name: str = Field(..., min_length=1, max_length=64, description="套房名称（自定义）")
    rooms: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ROOM_NAMES),
        description="房间名列表（0~24 个，不能重名）。"
        "缺省为空：房间在「编辑房子」里画框时命名，后端不再生成占位。",
    )
    timezone: str = Field("Asia/Shanghai", description="时区")


class HomeCreatedOut(BaseModel):
    home_id: int
    name: str
    rooms: list[dict[str, str]] = Field(default_factory=list, description="[{code, name}]")
    message: str


class HomeDeletedItem(BaseModel):
    """删除结果：删了多少数据，让用户知道"我删掉了什么"。"""

    home_id: int
    name: str
    deleted: dict[str, int] = Field(default_factory=dict)
    floorplan_removed: bool = False
    message: str


class RoomCreateIn(BaseModel):
    """在「编辑户型」里新增一个房间。"""

    name: str = Field(..., min_length=1, max_length=24, description="房间名（可随时再改）")


class RoomCreatedOut(BaseModel):
    room_id: int
    code: str
    name: str
    message: str


class HomeRenameIn(BaseModel):
    """给套房改名（「编辑房子」里改）。"""

    name: str = Field(..., min_length=1, max_length=64, description="新的套房名称")


class HomeRenamedOut(BaseModel):
    home_id: int
    name: str
    changed: bool = Field(..., description="名字实际有没有变")
    message: str


class RoomDeletedOut(BaseModel):
    room_id: int
    code: str
    name: str
    devices: int = Field(0, description="随房间一起删掉的设备数")
    deleted: dict[str, int] = Field(default_factory=dict, description="各表清理行数")
    message: str
