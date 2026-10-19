"""设备直接控制端点的请求/响应模型（M6）。

⚠️ 措辞上的红线（贯穿这个模块的所有 message 字段）
--------------------------------------------------
**"已下发指令" ≠ "设备状态已变"。** 本项目的控制链路是：

    接口 → executor.create_command（写指令记录） → MQTT publish → 设备/模拟器 → cmd_ack

接口返回时，最远只走到"MQTT 已经发出去"。设备是不是真的照做了、状态是不是真的变了，
要看 `cmd_ack` 回执（落在 `device_commands.result_message` 里）。
所以响应里必须**分开表达**这两件事：

- `status`：这条**指令**走到了哪一步（executed / pending_confirm / failed）
- `acknowledged`：设备**是否已回报**（收到 cmd_ack）

前端绝不能把 `status == "executed"` 直接渲染成"已开启"。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class DeviceCommandIn(BaseModel):
    """一条手动控制指令。"""

    capability: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="能力编码，如 power_on / brightness / target_temp。必须在该设备的能力档案里且 writable=true",
    )
    value: Any = Field(
        ...,
        description="目标值。类型必须与能力档案的 value_type 一致：bool / int / float / enum",
    )
    issued_by: str | None = Field(
        None, max_length=64, description="谁发的（大屏 / 手机端 / 脚本）。缺省记为 dashboard"
    )
    note: str | None = Field(None, max_length=200, description="备注，会一并写进指令记录")


class DeviceCommandOut(BaseModel):
    """一次手动控制的结果。

    ⚠️ 高风险设备（门锁 / 燃气阀 / 电气总闸）**永远不会**返回 `status="executed"` ——
    它们只会是 `pending_confirm`。这条不是靠这个接口自觉，而是 `executor.create_command`
    内部会强制改写（数据库 `device_commands.status` 的默认值也是 `pending_confirm`，双保险）。
    """

    device_id: int
    device_code: str
    device_name: str
    room_name: str
    capability: str
    capability_label: str
    value_type: str
    unit: str | None = None

    requested_value: Any = None
    applied_value: Any = None
    previous_value: Any = None

    # 指令走到了哪一步：executed / pending_confirm / failed
    status: str
    risk_level: str
    high_risk: bool = False
    command_id: int | None = None
    attempts: int = 0

    # 设备是否已回报（cmd_ack）。下发成功时通常还是 False，等一下再查
    acknowledged: bool = False

    message: str = ""


class CommandRecordOut(BaseModel):
    """一条历史指令记录（含回执）。"""

    id: int
    device_id: int
    capability: str
    params: dict[str, Any] | None = None
    issued_by: str | None = None
    risk_level: str
    status: str
    acknowledged: bool = False
    executed_at: dt.datetime | None = None
    created_at: dt.datetime | None = None
    result_message: str | None = None


class CommandListOut(BaseModel):
    device_id: int
    device_code: str
    device_name: str
    items: list[CommandRecordOut] = Field(default_factory=list)
    total: int = 0
