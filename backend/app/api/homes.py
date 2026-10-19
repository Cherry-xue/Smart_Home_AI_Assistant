"""`GET /api/homes/{home_id}/rooms` —— 房间卡片墙（家庭总览页的数据源）。

⚠️ 注意这里用 `def` 而不是 `async def`
--------------------------------------
端点内部要查库（告警 / 事件计数），而 SQLAlchemy 是**同步**的。
写成 `async def` 会在事件循环里直接做阻塞 IO，把整个后端卡住
（症状是"一个人查历史数据，所有人都卡"）。
写成 `def`，FastAPI 会自动把它丢到线程池 ——
**这条约定适用于本项目所有需要查库的端点。**
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_cache, get_catalog
from app.db.catalog import DeviceCatalog
from app.db.session import engine, get_db
from app.mqtt.cache import LatestValueCache
from app.schemas.common import (
    HomeRenameIn,
    HomeRenamedOut,
    RoomCreateIn,
    RoomCreatedOut,
    RoomDeletedOut,
    HomeCreateIn,
    HomeCreatedOut,
    HomeDeletedItem,
    HomeOut,
    HomesOut,
    RoomsOut,
)
from app.services.homes_admin import (
    HomeAdminError,
    create_home,
    create_room,
    delete_home,
    delete_room,
    rename_home,
)
from app.services.overview import DEFAULT_OFFLINE_AFTER_SEC, _online_state, room_cards

router = APIRouter(tags=["overview"])


@router.get("/homes", response_model=HomesOut, summary="套房列表（顶部切换数据源）")
def list_homes(
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
):
    """列出所有套房及其房间/设备/在线数。

    ⚠️ 在线判定复用 `overview._online_state`，不在这里另写一套 ——
    两处口径一旦不同，就会出现"房间卡片说在线、套房列表说离线"。
    """
    rows = db.execute(text("SELECT id, name, timezone FROM homes ORDER BY id")).all()
    now = dt.datetime.now()

    items = []
    for h in rows:
        devs = [d for d in catalog.all() if d.home_id == h.id]
        online = sum(
            1 for d in devs if _online_state(cache, d, now, DEFAULT_OFFLINE_AFTER_SEC)[0]
        )
        items.append(
            HomeOut(
                id=h.id,
                name=h.name,
                timezone=h.timezone,
                rooms=len(catalog.rooms_of(h.id)),
                devices=len(devs),
                online=online,
            )
        )
    return HomesOut(items=items, total=len(items))


@router.get(
    "/homes/{home_id}/rooms",
    response_model=RoomsOut,
    summary="房间卡片墙（每间房的关键指标、在线设备数、告警摘要、数据更新时间）",
)
def list_rooms(
    home_id: int,
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
):
    # ⚠️ 只有"套房不存在"才 404。
    #    套房存在但一个房间都没建（新建套房就是这样）必须返回 200 + 空列表 ——
    #    返回 404 会让前端 `Promise.all` 整体失败，页面停在上一套房的画面上。
    exists = db.execute(text("SELECT 1 FROM homes WHERE id = :h"), {"h": home_id}).first()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"家庭 {home_id} 不存在")
    items = room_cards(db, catalog, cache, home_id)
    return RoomsOut(home_id=home_id, generated_at=dt.datetime.now(), items=items)


def _reload_catalog(request: Request) -> DeviceCatalog:
    """新建/删除套房后必须刷新设备目录。

    ⚠️ 就地 `refresh`，不能换成新对象 —— 采集器（`Ingestor`）持有的是
    同一个目录引用，换对象会导致"新套房的报文被判成未知设备"。
    同一套注意事项见 `app/api/device_admin.py:_reload_catalog`。
    """
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        catalog = DeviceCatalog.load(engine)
        request.app.state.catalog = catalog
    else:
        catalog.refresh(engine)
    return catalog


@router.post(
    "/homes",
    response_model=HomeCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建套房（自定义名称 + 初始房间）",
)
def create_home_endpoint(
    body: HomeCreateIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """建一套空房（还没有设备与户型）。

    新套房**不继承**任何户型 —— 户型是"这套房实际的墙在哪"，
    套用别人的户型只会画出一张对不上的图。前端会提示去「编辑户型」画。
    """
    try:
        data = create_home(db, name=body.name, room_names=body.rooms, timezone=body.timezone)
    except HomeAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    _reload_catalog(request)
    rooms_text = "、".join(r["name"] for r in data["rooms"])
    # 新套房默认没有房间（也不给占位），所以这里要分两条路说
    if rooms_text:
        msg = f"已创建「{data['name']}」，房间：{rooms_text}。"
    else:
        msg = f"已创建「{data['name']}」。"
    msg += "下一步点顶栏「编辑」画房间（拖框 + 起名），再回来添加设备。"
    return HomeCreatedOut(
        home_id=data["home_id"],
        name=data["name"],
        rooms=data["rooms"],
        message=msg,
    )


@router.delete(
    "/homes/{home_id}",
    response_model=HomeDeletedItem,
    summary="删除套房（连同设备、房间、历史数据）",
)
def delete_home_endpoint(
    home_id: int,
    request: Request,
    db: Session = Depends(get_db),
    confirm: bool = Query(False, description="必须显式传 true，避免误点"),
):
    """⚠️ 不可恢复：设备读数、事件、告警、能耗、户型文件都会被删掉。"""
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "删除套房会连同设备与全部历史数据一起删除，请带 confirm=true 再调一次",
        )
    try:
        data = delete_home(db, home_id)
    except HomeAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    _reload_catalog(request)
    deleted = data["deleted"]
    # 返回给用户看的文案不能直接甩表名（"清理了 device_readings 3286945 条"没人想看）。
    # 固定顺序 + 中文标签，数量为 0 的不提。
    label_of = {
        "devices": "设备",
        "rooms": "房间",
        "device_readings": "读数",
        "sensor_events": "事件",
        "device_commands": "指令",
        "energy_readings": "能耗记录",
        "alarms": "告警",
        "care_observations": "照护记录",
        "suggestions": "建议",
        "memories": "记忆",
        "conversations": "对话",
        "disturbance_rules": "免打扰规则",
        "device_capabilities": "设备能力",
    }
    parts = [
        f"{label_of.get(k, k)} {v:,} 条"
        for k, v in sorted(deleted.items(), key=lambda kv: list(label_of).index(kv[0]) if kv[0] in label_of else 99)
        if v
    ]
    return HomeDeletedItem(
        home_id=data["home_id"],
        name=data["name"],
        deleted=deleted,
        floorplan_removed=data["floorplan_removed"],
        message=f"已删除「{data['name']}」"
        + (f"，清理了 {'、'.join(parts)}" if parts else "")
        + ("，户型文件已一并删除" if data["floorplan_removed"] else ""),
    )


@router.post(
    "/homes/{home_id}/rooms",
    response_model=RoomCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="在套房里新增一个房间（户型编辑器用）",
)
def create_room_endpoint(
    home_id: int,
    body: RoomCreateIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """房间编码由后端生成（`room_N`）：编码要进 MQTT topic，不接受用户输入。"""
    try:
        data = create_room(db, home_id, body.name)
    except HomeAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    # 房间进目录后，房间卡片墙 / 设备列表能立刻看到它
    _reload_catalog(request)
    return RoomCreatedOut(
        room_id=data["room_id"],
        code=data["code"],
        name=data["name"],
        message=f"已新增房间「{data['name']}」，现在可以在图上拖框并指定给它。",
    )


@router.patch(
    "/homes/{home_id}",
    response_model=HomeRenamedOut,
    summary="给套房改名",
)
def rename_home_endpoint(
    home_id: int,
    body: HomeRenameIn,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        data = rename_home(db, home_id, body.name)
    except HomeAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    _reload_catalog(request)
    return HomeRenamedOut(
        home_id=data["home_id"],
        name=data["name"],
        changed=data["changed"],
        message=f"已改名为「{data['name']}」" if data["changed"] else "名字没有变化",
    )


@router.delete(
    "/homes/{home_id}/rooms/{code}",
    response_model=RoomDeletedOut,
    summary="删除房间（连同房间里的设备与历史数据）",
)
def delete_room_endpoint(
    home_id: int,
    code: str,
    request: Request,
    db: Session = Depends(get_db),
    confirm: bool = Query(False, description="必须显式传 true，避免误删"),
):
    """⚠️ 不可恢复：房间里的设备、读数、事件、指令、能耗、告警都会被删掉。"""
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "删除房间会连同里面的设备与历史数据一起删除，请带 confirm=true 再调一次",
        )
    try:
        data = delete_room(db, home_id, code)
    except HomeAdminError as e:
        raise HTTPException(e.status_code, detail=e.message) from e

    _reload_catalog(request)
    dev = data["devices"]
    parts = [f"{k} {v} 行" for k, v in data["deleted"].items() if v]
    msg = f"已删除房间「{data['name']}」" + (f"及其 {dev} 台设备" if dev else "")
    msg += "。" + (f"清理了 {'、'.join(parts)}。" if parts else "")
    return RoomDeletedOut(
        room_id=data["room_id"],
        code=data["code"],
        name=data["name"],
        devices=dev,
        deleted=data["deleted"],
        message=msg,
    )
