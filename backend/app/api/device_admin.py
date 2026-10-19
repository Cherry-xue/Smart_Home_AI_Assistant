"""预设设备与设备增删端点。

```
GET  /api/device-presets              列出可添加的预设设备
POST /api/devices                     按预设新增一台设备
POST /api/devices/{device_id}/enabled 启用 / 停用（软删）
```

⚠️ 路径为什么是 `/api/device-presets` 而不是 `/api/devices/presets`：
`api/devices.py` 里已经注册了 `GET /api/devices/{device_id}`，
而 FastAPI 按**声明顺序**匹配 —— `/devices/presets` 会先被 `{device_id}` 吃掉
（然后因为 "presets" 不是整数报 422）。所以预设清单单独走一个顶层路径。

⚠️ 三处**写操作之后都必须重载设备目录**：`app.state.catalog` 只在进程启动时
`DeviceCatalog.load()` 赋一次，不重载的话"加完设备不重启看不到"。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_catalog
from app.db.catalog import DeviceCatalog
from app.db.session import engine, get_db
from app.schemas.device_admin import (
    DeviceCreatedOut,
    DeviceCreateIn,
    DeviceEnabledIn,
    DeviceEnabledOut,
    DevicePurgedOut,
    DevicePurgeIn,
    DisabledDeviceListOut,
    DisabledDeviceOut,
    PresetListOut,
    PresetOut,
)
from app.services.device_admin import (
    DeviceAdminError,
    add_device,
    build_purge_message,
    list_disabled,
    load_presets,
    purge_device,
    set_device_enabled,
)

log = logging.getLogger("app.api.device_admin")

presets_router = APIRouter(prefix="/device-presets", tags=["devices"])
router = APIRouter(prefix="/devices", tags=["devices"])
#: 已停用设备单独一个顶层路径。
#: ⚠️ 不能用 `/devices/disabled` —— 会被 `GET /devices/{device_id}` 先吃掉（见文件头说明）。
disabled_router = APIRouter(prefix="/disabled-devices", tags=["devices"])


def _reload_catalog(request: Request) -> DeviceCatalog:
    """让设备目录重新读库。

    ⚠️⚠️ **必须就地刷新同一个对象，不能换成新对象。**
    目录被多个长生命周期组件持有引用（`Ingestor.catalog`、`QualityEvaluator`、
    启动预热逻辑），只换 `app.state.catalog` 的话，API 能看到新设备，
    **但采集器还在用旧目录** → 新设备的报文被判成"未知设备"全部拒收。
    现象是"界面上加进来了、生成器也在产数据，但库里一行都没有"
    （2026-10-16 实测踩到，api.log 刷了 26 条「未知设备 light_1」）。
    """
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        catalog = DeviceCatalog.load(engine)
        request.app.state.catalog = catalog
    else:
        catalog.refresh(engine)
    log.info("设备目录已刷新：%d 台设备 / %d 个房间", len(catalog), len(catalog.rooms))
    return catalog


# ============================================================ 预设清单
@presets_router.get(
    "",
    response_model=PresetListOut,
    summary="可添加的预设设备清单（用户只能从这里选）",
)
def list_presets() -> PresetListOut:
    """返回 `config/device_presets.yaml` 的内容。

    ⚠️ `high_risk` 是**只读展示**：它由后端按 `device_type` 推导，
    新增设备时请求里传什么都不影响结果（`DeviceCreateIn` 里根本没有这个字段）。
    """
    try:
        items = [PresetOut(**p.as_dict()) for p in load_presets()]
    except DeviceAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e
    return PresetListOut(items=items, total=len(items))


# ============================================================ 新增
@router.post(
    "",
    response_model=DeviceCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="按预设新增一台设备",
)
def create_device(
    body: DeviceCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
) -> DeviceCreatedOut:
    """按预设新增设备。

    - 只能选**已有预设**（不能自定义类型/能力）
    - **`high_risk` 由 `device_type` 推导**，请求无法覆盖
    - 新增后立刻重载设备目录，接口马上就能查到它
    - ⚠️ 生成器是独立常驻进程，它会**周期性重扫**设备表（约 30 秒）；
      在此之前新设备在界面上"在线但无数据"，这是正常的
    """
    try:
        device, preset = add_device(
            db,
            preset_key=body.preset_key,
            room_id=body.room_id,
            name=body.name,
            code=body.code,
        )
    except DeviceAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    _reload_catalog(request)

    room_name = ""
    dev = request.app.state.catalog.by_id(device.id)
    if dev is not None:
        room_name = dev.room_name

    warn = " ⚠️ 该类型属于高风险，任何指令都只会生成待确认项。" if preset.high_risk else ""
    return DeviceCreatedOut(
        id=device.id,
        code=device.code,
        name=device.name,
        device_type=device.device_type,
        room_id=device.room_id,
        room_name=room_name,
        high_risk=preset.high_risk,
        enabled=bool(device.enabled),
        preset_key=preset.key,
        capability_count=len(preset.capabilities),
        message=(
            f"已新增「{device.name}」。生成器会在约 30 秒内开始为它产数据"
            f"（在那之前它会显示为在线但无读数）。{warn}"
        ),
    )


# ============================================================ 启停（软删）
@router.post(
    "/{device_id}/enabled",
    response_model=DeviceEnabledOut,
    summary="启用 / 停用设备（停用是软删，历史数据保留）",
)
def set_enabled(
    device_id: int,
    body: DeviceEnabledIn,
    request: Request,
    db: Session = Depends(get_db),
) -> DeviceEnabledOut:
    """停用 = 软删：把 `devices.enabled` 置 0。

    为什么不做硬删：`device_commands` / `sensor_events` 的外键是
    `ON DELETE NO ACTION`（删除会报 1451），而且 `device_readings`
    （百万行级）没有指向 devices 的外键，硬删会留下孤儿行。
    软删则零成本 —— 后端目录与生成器的 SQL 都带 `WHERE d.enabled = 1`。
    """
    try:
        device = set_device_enabled(db, device_id, body.enabled)
    except DeviceAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    _reload_catalog(request)

    return DeviceEnabledOut(
        id=device.id,
        code=device.code,
        name=device.name,
        enabled=bool(device.enabled),
        message=(
            f"已启用「{device.name}」，生成器会在约 30 秒内恢复为它产数据。"
            if device.enabled
            else f"已停用「{device.name}」。历史读数和指令审计都保留着，随时可以重新启用。"
        ),
    )


# ============================================================ 已停用设备
@disabled_router.get(
    "",
    response_model=DisabledDeviceListOut,
    summary="已停用（软删）的设备，可从这里恢复",
)
def list_disabled_devices(db: Session = Depends(get_db)) -> DisabledDeviceListOut:
    """为什么需要它：`GET /api/devices` 走设备目录，而目录的 SQL 带
    `WHERE d.enabled = 1` —— 停用之后设备会从列表里消失，**用户再也点不到"恢复"**。
    所以这里直接查库，把已停用的设备单独列出来。
    """
    items = list_disabled(db, home_id=1)
    return DisabledDeviceListOut(
        items=[DisabledDeviceOut(**x) for x in items], total=len(items)
    )


# ============================================================ 彻底删除
@router.post(
    "/{device_id}/purge",
    response_model=DevicePurgedOut,
    summary="彻底删除设备（不可逆，会一并清掉读数与指令审计）",
)
def purge(
    device_id: int,
    body: DevicePurgeIn,
    request: Request,
    db: Session = Depends(get_db),
) -> DevicePurgedOut:
    """**真正把设备从库里删掉** —— 与「停用」（软删）相对。

    ⚠️ 三重保险，别绕过：

    1. **必须先停用**（`enabled=0`）。刚停用又立刻彻底删，多半是没想清楚；
       强制多一步，也保证"删掉的一定是不再用的设备"。
    2. **必须传 `acknowledge_audit_loss=true`**。指令记录里存着高风险动作的
       "人工确认留痕"，删掉就没了 —— 这是本项目**唯一**允许真正销毁审计数据的入口。
    3. 依赖表清单**运行时从 information_schema 查**，不写死
       （本项目有 6 张表带 device_id，凭印象一定会漏）。

    返回体里带**逐表删除行数**和**受影响的场景**，用户能当场看到删干净没有。
    """
    try:
        report = purge_device(db, device_id, acknowledge_audit_loss=body.acknowledge_audit_loss)
    except DeviceAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    _reload_catalog(request)

    return DevicePurgedOut(**report, message=build_purge_message(report))
