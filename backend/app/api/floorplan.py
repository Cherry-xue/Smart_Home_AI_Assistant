"""户型图：按套房读取 / 保存几何、上传底图。

三条设计取舍，都写在代码里防止以后被"优化"掉
------------------------------------------
1. **几何存文件不存库**：`rooms` 表没有几何字段，加字段需要 DDL，而 `app_rw`
   没有 DDL 权限（既定约束）。路径按套房隔离：`config/floorplans/{home_id}.json`。

2. **房间码必须过 `rooms` 表校验**：它进 MQTT topic，写错等于把读数挂到不存在的房间。
   `is_circulation=true` 的交通空间是唯一例外（库里确实没有"走廊"这个房间）。

3. **房间名只有一个真相：`rooms.name`**。保存时如果名字变了就写回数据库，
   然后**就地刷新设备目录** —— 否则设备页还显示旧名字（和"加了设备却看不见"同源）。

4. **底图要重新编码**：不信任 content-type 和扩展名，用 Pillow 真解一遍；
   顺带清掉 EXIF 并统一成 PNG。**刻意不收 SVG** —— SVG 可以带脚本。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import engine, get_db
from app.schemas.floorplan import FloorplanIn, FloorplanOut, FloorplanSaveOut
from app.services.floorplan import (
    FloorplanError,
    background_path,
    load_floorplan,
    remove_background,
    reset_floorplan,
    save_background,
    save_floorplan,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/floorplan", tags=["floorplan"])

HOME_ID_Q = Query(1, description="套房 id（默认 1 = 演示家庭）")


def _refresh_catalog(request: Request) -> None:
    """房间可能被改名了 → 就地刷新设备目录（换对象会让采集器继续用旧目录）。"""
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is not None:
        catalog.refresh(engine)


@router.get("", response_model=FloorplanOut, summary="当前户型几何（含底图与未画出的房间）")
def get_floorplan(home_id: int = HOME_ID_Q, db: Session = Depends(get_db)):
    return load_floorplan(db, home_id)


@router.put(
    "",
    response_model=FloorplanSaveOut,
    summary="保存户型几何（房间码会按 rooms 表校验；改名会写回数据库）",
)
def put_floorplan(payload: FloorplanIn, request: Request, home_id: int = HOME_ID_Q,
                  db: Session = Depends(get_db)):
    try:
        data = save_floorplan(db, payload.model_dump(), home_id)
    except FloorplanError as e:
        raise HTTPException(
            status_code=400, detail={"message": str(e), "fields": e.fields}
        ) from e

    _refresh_catalog(request)

    drawn = len([a for a in data["areas"] if not a["is_circulation"]])
    circ = len(data["areas"]) - drawn
    # 走廊不建房间记录，但用户画了它 —— 分开数，否则"画了 4 个只说 2 个"很像丢数据
    msg = f"已保存 {drawn} 个房间" + (f" + {circ} 段走廊" if circ else "") + " 的框选"
    if data["missing_rooms"]:
        names = "、".join(r["name"] for r in data["missing_rooms"])
        msg += f"；注意：{names} 没有画到图上，这些房间的设备不会显示在户型图里"
    return FloorplanSaveOut(message=msg, floorplan=FloorplanOut(**data))


@router.post(
    "/image",
    response_model=FloorplanSaveOut,
    summary="上传户型底图（PNG/JPEG/WEBP，会自动归一化为 PNG）",
)
async def upload_image(file: UploadFile = File(...), home_id: int = HOME_ID_Q,
                       db: Session = Depends(get_db)):
    raw = await file.read()
    try:
        data = save_background(db, raw, home_id)
    except FloorplanError as e:
        raise HTTPException(
            status_code=400, detail={"message": str(e), "fields": e.fields}
        ) from e

    bg = data.get("background") or {}
    return FloorplanSaveOut(
        message=(
            f"底图已保存（{bg.get('width')}×{bg.get('height')}），画布已按图片尺寸重设。"
            f"接下来在图上拖拽框出每个房间并指定房间码。"
        ),
        floorplan=FloorplanOut(**data),
    )


@router.get(
    "/image",
    summary="获取户型底图（二进制）",
    responses={200: {"content": {"image/png": {}}}},
)
def get_image(home_id: int = HOME_ID_Q):
    path = background_path(home_id)
    if path is None:
        raise HTTPException(status_code=404, detail="还没有上传底图")
    # 文件名带版本由查询参数负责，这里给短缓存即可（换了图 URL 会变）
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=300"})


@router.delete(
    "/image",
    response_model=FloorplanSaveOut,
    summary="删除户型底图（只删图，框选与房间都保留）",
)
def delete_image(home_id: int = HOME_ID_Q, db: Session = Depends(get_db)):
    """⚠️ 只删底图文件与它的元信息：`view_box`、框选、房间都不动，坐标不会错位。"""
    try:
        data = remove_background(db, home_id)
    except FloorplanError as e:
        raise HTTPException(
            status_code=400, detail={"message": str(e), "fields": e.fields}
        ) from e

    return FloorplanSaveOut(
        message="底图已删除，框选与房间都保留了。画布尺寸不变，可以重新上传底图。",
        floorplan=FloorplanOut(**data),
    )


@router.post(
    "/reset",
    response_model=FloorplanSaveOut,
    summary="恢复默认户型（可选保留底图、只清空框选）",
)
def reset(
    keep_background: bool = Query(
        False, description="true = 只清空框选、保留已上传的底图（图对、框错了的场景）"
    ),
    home_id: int = HOME_ID_Q,
    db: Session = Depends(get_db),
):
    data = reset_floorplan(db, home_id, keep_background=keep_background)
    if keep_background:
        msg = "已清空框选，底图保留 —— 请重新框选房间"
    elif home_id == 1:
        msg = "已恢复为内置默认户型（底图也一并删除）"
    else:
        msg = "已清空该套房的户型（底图也一并删除），现在是空画布，请上传底图并框选"
    return FloorplanSaveOut(message=msg, floorplan=FloorplanOut(**data))
