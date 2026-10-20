"""`GET /api/energy/summary?from=&to=` —— 能耗汇总（账单口径）。

金额一律由确定性代码按分时电价算出（`core/tariff.py`），
**不是模型估的** —— 这是规划书的硬要求：涉及钱的地方不能让模型心算。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_cache, get_catalog
from app.core.ttl_cache import TTLCache
from app.core.tariff import Tariff, get_tariff
from app.db.catalog import DeviceCatalog
from app.db.session import get_db
from app.mqtt.cache import LatestValueCache
from app.schemas.system import EnergySummaryOut
from app.services.analytics import energy_summary, resolve_window
from app.services.overview import data_anchor

router = APIRouter(prefix="/energy", tags=["energy"])

#: 能耗汇总是**慢变量**：24 小时窗口上要做三次聚合，实测 1.1 秒。
#: 20 秒内的重复请求直接复用（切页 / 总览与能耗页同时取数都会命中）。
#: key 用**请求参数**而不是解析后的窗口 —— 缺省窗口以"数据锚点"为基准，
#: 锚点每 tick 都在前进，用窗口当 key 会永远命中不了。
_ENERGY_CACHE = TTLCache(ttl=20.0)


@router.get(
    "/summary",
    response_model=EnergySummaryOut,
    summary="能耗汇总：总量 / 分时电价 / 高耗设备 / 逐小时",
)
def energy_summary_endpoint(
    from_: dt.datetime | None = Query(
        None, alias="from", description="起始时间（ISO 8601）。缺省 = 数据最新时间往前 24 小时"
    ),
    to_: dt.datetime | None = Query(None, alias="to", description="结束时间。缺省 = 数据最新时间"),
    home_id: int = Query(
        1,
        description="套房 id，缺省 1。⚠️ 缺省不为 None 是为了让旧调用方（不传参数）"
        "仍然只看到 1 号房的电费，而不是两套房加在一起",
    ),
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
    tariff: Tariff = Depends(get_tariff),
):
    # 与 /readings 一致：缺省窗口以数据最新时间为锚点（模拟数据的时间轴可能不在"现在"）
    window = resolve_window(from_, to_, default_hours=24.0, anchor=data_anchor(catalog, cache))
    key = (home_id, from_.isoformat() if from_ else "auto", to_.isoformat() if to_ else "auto")
    return _ENERGY_CACHE.get_or(
        key,
        lambda: energy_summary(
            db, catalog, tariff, from_dt=window[0], to_dt=window[1], home_id=home_id
        ),
    )
