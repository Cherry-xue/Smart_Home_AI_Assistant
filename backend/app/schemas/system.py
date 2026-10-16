"""API 数据模型：能耗与系统状态。"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 能耗
class PeriodUsageOut(BaseModel):
    period: str | None = Field(None, description="peak / flat / valley")
    price: float | None = Field(None, description="该时段单价（元/kWh）")
    kwh: float
    cost: float | None = None
    share: float | None = Field(None, description="占总用电量的比例")
    avg_power_w: float | None = None
    peak_power_w: float | None = None
    rows: int = 0


class DeviceUsageOut(BaseModel):
    device_id: int
    code: str
    name: str
    device_type: str
    room_name: str | None = None
    kwh: float
    cost: float
    share: float | None = None
    avg_power_w: float | None = None
    peak_power_w: float = 0.0


class HourlyUsageOut(BaseModel):
    hour: int
    kwh: float
    avg_power_w: float | None = None


class EnergySummaryOut(BaseModel):
    from_: dt.datetime = Field(..., alias="from")
    to: dt.datetime
    currency: str = "CNY"
    unit: str = "kWh"
    total_kwh: float
    total_cost: float
    by_period: list[PeriodUsageOut] = Field(default_factory=list)
    by_device: list[DeviceUsageOut] = Field(default_factory=list)
    hourly: list[HourlyUsageOut] = Field(default_factory=list)
    observations: list[str] = Field(
        default_factory=list, description="由确定性代码算出的事实陈述（不含建议）"
    )
    note: str | None = None

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------- 系统状态
class DatabaseHealth(BaseModel):
    ok: bool
    version: str | None = None
    charset: str | None = None
    current_user: str | None = None
    response_ms: float | None = None
    error: str | None = None


class MqttHealth(BaseModel):
    enabled: bool
    connected: bool = False
    thread_alive: bool = False
    uptime_sec: float = 0.0
    sessions: int = 0
    reconnects: int = 0
    messages_seen: int = 0
    last_message_at: dt.datetime | None = None
    error: str | None = None


class IngestHealth(BaseModel):
    received: int = 0
    rejected: int = 0
    unknown_device: int = 0
    duplicates: int = 0
    stale: int = 0
    out_of_range: int = 0
    retained_snapshots: int = 0
    readings: int = 0
    energies: int = 0
    events: int = 0
    errors: int = 0
    last_error: str | None = None


class CacheHealth(BaseModel):
    devices: int = 0
    values: int = 0
    preloaded: int = Field(0, description="启动时从库里预热进来的读数条数")
    offline_count: int = 0
    offline_devices: list[str] = Field(
        default_factory=list, description="超过阈值未上报的设备编码（最多列前 10 个）"
    )


class HealthOut(BaseModel):
    status: str = Field(..., description="ok / degraded")
    app_env: str
    data_mode: str = Field(..., description="mock / mqtt / csv / real_gateway")
    timezone: str
    generated_at: dt.datetime
    data_anchor: dt.datetime | None = Field(
        None,
        description="数据时间锚点 = 手上最新数据的观测时间。模拟器按倍速跑，"
        "数据时间可能不在『现在』附近 —— 调用方（前端）用它判断数据实际到哪了，"
        "缺省时间窗也是以它为基准的",
    )
    database: DatabaseHealth
    mqtt: MqttHealth
    ingest: IngestHealth
    cache: CacheHealth
    devices: dict[str, int] = Field(default_factory=dict)
    tariff: str
    high_risk_auto_exec: bool = Field(
        False, description="高风险自动执行开关。恒为 false —— 即使 .env 改成 true 也不放行"
    )
