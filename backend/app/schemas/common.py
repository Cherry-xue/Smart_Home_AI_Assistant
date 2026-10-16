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
    row_count: int
    stale_rows: int
    quality_summary: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, MetricSeriesOut] = Field(default_factory=dict)
    note: str | None = None

    model_config = {"populate_by_name": True}
