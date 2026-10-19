"""预设设备与设备增删的请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PresetCapabilityOut(BaseModel):
    code: str
    display_name: str
    value_type: str
    unit: str | None = None
    value_min: Any = None
    value_max: Any = None
    enum_values: Any = None
    writable: bool = False
    risk_level: str = "low"


class PresetOut(BaseModel):
    """一条预设设备。用户只能从这里选。"""

    key: str
    label: str
    device_type: str
    note: str = ""
    #: ⚠️ 只读展示用。后端按 device_type 推导，**请求里传了也不生效**
    high_risk: bool = False
    capability_count: int = 0
    writable_count: int = 0
    capabilities: list[PresetCapabilityOut] = Field(default_factory=list)


class PresetListOut(BaseModel):
    items: list[PresetOut] = Field(default_factory=list)
    total: int = 0


class DeviceCreateIn(BaseModel):
    """新增一台设备。⚠️ **没有 high_risk 字段** —— 那是按类型推导的，不允许外部指定。"""

    preset_key: str = Field(..., min_length=1, description="预设 key，取自 /api/device-presets")
    room_id: int = Field(..., description="放进哪个房间")
    name: str | None = Field(None, max_length=64, description="设备名。留空则用「房间名+预设名」")
    code: str | None = Field(
        None,
        max_length=64,
        description="设备编码（会进 MQTT topic，ASCII 小写）。留空自动生成",
    )


class DeviceCreatedOut(BaseModel):
    id: int
    code: str
    name: str
    device_type: str
    room_id: int | None = None
    room_name: str = ""
    high_risk: bool = False
    enabled: bool = True
    preset_key: str = ""
    capability_count: int = 0
    #: 让界面能如实告诉用户"要做什么才生效"
    message: str = ""


class DeviceEnabledIn(BaseModel):
    enabled: bool = Field(..., description="true=启用，false=停用（软删，历史数据保留）")


class DeviceEnabledOut(BaseModel):
    id: int
    code: str
    name: str
    enabled: bool
    message: str = ""


class DisabledDeviceOut(BaseModel):
    """一台已停用（软删）的设备。

    ⚠️ 故意**不带最新读数**：设备已停用，它的值不该再被当成有效状态展示 ——
    否则界面上会出现一个"已停用但还显示温度 24℃"的卡片，那是误导。
    """

    id: int
    code: str
    name: str
    device_type: str
    room_name: str = ""
    high_risk: bool = False


class DisabledDeviceListOut(BaseModel):
    items: list[DisabledDeviceOut] = Field(default_factory=list)
    total: int = 0


class DevicePurgeIn(BaseModel):
    """彻底删除一台设备（不可逆）。"""

    acknowledge_audit_loss: bool = Field(
        False,
        description="必须显式传 true —— 确认知道指令审计记录（含高风险动作的人工确认留痕）会一并删除且不可恢复",
    )


class DevicePurgedOut(BaseModel):
    """删除结果报告。

    ⚠️ 刻意把 `deleted`（每张表删了多少行）和 `affected_scenes` 一起返回：
    删除是不可逆的，"删干净没有""有没有留下悬空引用"必须让用户当场看见，
    不能只回一句"删除成功"。
    """

    id: int
    code: str
    name: str
    device_type: str
    high_risk: bool = False
    room_id: int | None = None
    deleted: dict[str, int] = Field(default_factory=dict)
    total_rows: int = 0
    affected_scenes: list[dict] = Field(default_factory=list)
    message: str = ""
