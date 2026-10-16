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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_cache, get_catalog
from app.db.catalog import DeviceCatalog
from app.db.session import get_db
from app.mqtt.cache import LatestValueCache
from app.schemas.common import RoomsOut
from app.services.overview import room_cards

router = APIRouter(tags=["overview"])


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
    if not catalog.rooms_of(home_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"家庭 {home_id} 不存在或没有房间"
        )
    items = room_cards(db, catalog, cache, home_id)
    return RoomsOut(home_id=home_id, generated_at=dt.datetime.now(), items=items)
