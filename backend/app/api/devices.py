"""设备相关端点：列表 / 详情 / 历史读数 / 能力档案。

路径与规划书 §10.3 的接口契约一致：
    GET /api/devices?room_id=&type=
    GET /api/devices/{device_id}/readings?from=&to=
    GET /api/devices/{device_id}/capabilities

所有端点都用 `def`（同步）—— 需要查库的用线程池，不需要查库的更快返回。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_cache, get_catalog, mqtt_link_down
from app.db.catalog import DeviceCatalog
from app.db.session import get_db
from app.mqtt.cache import LatestValueCache
from app.schemas.common import (
    DeviceCapabilitiesOut,
    DeviceListMeta,
    DeviceListOut,
    DeviceOut,
    DeviceReadingsOut,
)
from app.services.analytics import device_readings as query_readings
from app.services.analytics import resolve_window
from app.services.overview import (
    data_anchor,
    device_capabilities,
    device_list,
    device_summary,
)

router = APIRouter(prefix="/devices", tags=["devices"])


def _resolve_room_code(
    catalog: DeviceCatalog, room_id: int | None, room_code: str | None
) -> str | None:
    """把契约里的 `room_id`（数字）换算成内部使用的房间编码。"""
    if room_id is None:
        return room_code
    room = next((r for r in catalog.rooms if r.id == room_id), None)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"房间 {room_id} 不存在")
    return room.code


def _require_device(catalog: DeviceCatalog, device_id: int):
    dev = catalog.by_id(device_id)
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"设备 {device_id} 不存在")
    return dev


@router.get("", response_model=DeviceListOut, summary="设备列表（含最新值与在线状态）")
def list_devices(
    room_id: int | None = Query(None, description="按房间 ID 过滤"),
    room_code: str | None = Query(None, description="按房间编码过滤（与 room_id 二选一）"),
    device_type: str | None = Query(None, alias="type", description="按设备类型过滤，如 light / ac / temp_sensor"),
    high_risk_only: bool = Query(False, description="只看高风险设备（门锁 / 燃气阀 / 总闸）"),
    online_only: bool = Query(False, description="只看在线设备"),
    home_id: int = Query(
        1,
        description="套房 id，缺省 1（演示家庭）。"
        "⚠️ 缺省值刻意不是 None：旧大屏等既有调用方不传这个参数，"
        "若默认返回全部套房，界面会突然多出别人家的设备。",
    ),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
    link_down: bool = Depends(mqtt_link_down),
):
    code = _resolve_room_code(catalog, room_id, room_code)
    items = device_list(
        catalog,
        cache,
        room_code=code,
        device_type=device_type,
        high_risk_only=high_risk_only,
        online_only=online_only,
        home_id=home_id,
        link_down=link_down,
    )
    return DeviceListOut(
        meta=DeviceListMeta(
            total=len(catalog),
            filtered=len(items),
            summary=device_summary(catalog, home_id),
            generated_at=dt.datetime.now(),
        ),
        items=items,
    )


@router.get("/{device_id}", response_model=DeviceOut, summary="单台设备详情")
def device_detail(
    device_id: int,
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
    link_down: bool = Depends(mqtt_link_down),
):
    dev = _require_device(catalog, device_id)
    # 详情页与列表必须同口径：列表说离线、详情说在线是最容易让人不信系统的 bug
    return device_list(catalog, cache, codes=[dev.code], link_down=link_down)[0]


@router.get(
    "/{device_id}/readings",
    response_model=DeviceReadingsOut,
    summary="历史读数（按指标分组，供画曲线）",
)
def device_readings_endpoint(
    device_id: int,
    from_: dt.datetime | None = Query(
        None, alias="from", description="起始时间（ISO 8601）。缺省 = 数据最新时间往前 24 小时"
    ),
    to_: dt.datetime | None = Query(None, alias="to", description="结束时间。缺省 = 数据最新时间"),
    metric: list[str] | None = Query(None, description="只取指定指标，可重复传该参数"),
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
):
    dev = _require_device(catalog, device_id)
    # 缺省窗口以【数据最新时间】为锚点：模拟器的时间轴可能不在"现在"附近，
    # 用"现在往前 24 小时"会一条都查不到（详见 overview.data_anchor）
    window = resolve_window(from_, to_, anchor=data_anchor(catalog, cache))
    return query_readings(db, dev, from_dt=window[0], to_dt=window[1], metrics=metric)


@router.get(
    "/{device_id}/capabilities",
    response_model=DeviceCapabilitiesOut,
    summary="能力档案（含取值范围、是否可写、风险等级）",
)
def device_capabilities_endpoint(
    device_id: int,
    catalog: DeviceCatalog = Depends(get_catalog),
):
    dev = _require_device(catalog, device_id)
    return device_capabilities(dev)
