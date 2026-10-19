"""设备控制端点（M6）。

```
POST /api/devices/{device_id}/command     给一台设备下发一条指令
GET  /api/devices/{device_id}/commands    查这台设备的指令记录（含设备回执）
```

设计说明
--------
放在独立文件里（而不是塞进 `api/devices.py`），是为了让"控制"这条路一眼可见、
也方便单独回滚：**后端原本只有两条控制通道**（对话、场景），这是第三条，
它的红线约束值得单独一处说明。

校验与执行在 `services/device_control.py`，那里会复用 `scenes/executor.execute_plan`，
所以高风险设备（门锁/燃气阀/总闸）在这个入口上**同样**只会生成待确认项。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_cache, get_catalog
from app.db.catalog import DeviceCatalog
from app.db.session import get_db
from app.mqtt.cache import LatestValueCache
from app.schemas.command import CommandListOut, DeviceCommandIn, DeviceCommandOut
from app.services.device_control import DeviceCommandError, list_commands, run_command

router = APIRouter(prefix="/devices", tags=["devices"])


def _require_device(catalog: DeviceCatalog, device_id: int):
    dev = catalog.by_id(device_id)
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"设备 {device_id} 不存在")
    return dev


@router.post(
    "/{device_id}/command",
    response_model=DeviceCommandOut,
    summary="给一台设备下发指令（高风险设备只生成待确认项）",
)
def send_command(
    device_id: int,
    body: DeviceCommandIn,
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
) -> DeviceCommandOut:
    """下发一条手动控制指令。

    ⚠️ 返回 `status="executed"` 只代表**指令已经发出去并留痕**，
    **不代表设备状态已经变了** —— 那要看 `acknowledged`（设备回执）。
    门锁 / 燃气阀 / 电气总闸永远只返回 `pending_confirm`。

    校验失败（能力不存在 404 / 不可写 / 值越界 / 类型不符 422）会明确报错，
    **不会静默夹取**：手动控制是用户明确指定的值，悄悄改掉等于骗人。
    """
    dev = _require_device(catalog, device_id)
    try:
        return run_command(
            db,
            catalog,
            cache,
            device=dev,
            capability=body.capability,
            raw_value=body.value,
            issued_by=body.issued_by,
            note=body.note,
        )
    except DeviceCommandError as e:
        raise HTTPException(e.status_code, detail=e.message) from e


@router.get(
    "/{device_id}/commands",
    response_model=CommandListOut,
    summary="某台设备的指令记录（新的在前，含设备回执标记）",
)
def device_commands(
    device_id: int,
    limit: int = Query(20, ge=1, le=100, description="最多返回几条"),
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
) -> CommandListOut:
    """用来回答"我点了开关，到底执行成没成"。

    `acknowledged=true` 表示设备已回报（`cmd_ack`）；
    `result_message` 里带着回执原文。**下发成功但没回执 = 还不能说状态变了。**
    """
    dev = _require_device(catalog, device_id)
    return list_commands(db, dev, limit=limit)
