"""M6 业务端点：安防告警 / 照护观察 / 主动建议。

三组端点共同的三条约定
--------------------
1. **读之前先推导一次**。`sync_*` 是幂等的（见各 service 的注释），
   所以列表接口默认先跑一遍推导再查 —— 用户不需要知道"有个后台任务在算"，
   点开就能看到当前状态。`sync=false` 可以关掉（调试/压测时用）。

2. **时间基准是数据锚点，不是真实时钟**。模拟数据的时间轴可能领先或落后于现在，
   用 `now` 做窗口基准会一条都查不到（这坑在 `/readings` 上已经踩过一次）。

3. **返回里带"这次算了什么"**（`sync` / `coverage`）。演示和答辩时最常被问的
   "这条告警哪来的、数据够不够"——答案直接在响应里，不用现场翻日志。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_cache, get_catalog
from app.core.insights_rules import InsightsRules, get_insights_rules
from app.core.tariff import Tariff, get_tariff
from app.db.catalog import DeviceCatalog
from app.db.session import get_db
from app.mqtt.cache import LatestValueCache
from app.schemas.insights import (
    AlarmAckIn,
    AlarmAckOut,
    AlarmListOut,
    AlarmSyncOut,
    ObservationListOut,
    SubjectCoverageOut,
    SuggestionActionOut,
    SuggestionListOut,
)
from app.services.analytics import resolve_window
from app.services.care import list_observations, sync_observations
from app.services.overview import data_anchor
from app.services.safety import (
    acknowledge_alarm,
    list_alarms,
    sync_event_alarms,
    sync_state_alarms,
)
from app.services.suggestions import (
    accept_suggestion,
    dismiss_suggestion,
    list_suggestions,
    sync_suggestions,
)

security_router = APIRouter(prefix="/security", tags=["security"])
care_router = APIRouter(prefix="/care", tags=["care"])
suggestions_router = APIRouter(prefix="/suggestions", tags=["suggestions"])


def _anchor(catalog: DeviceCatalog, cache: LatestValueCache) -> dt.datetime:
    """数据时间锚点；一条数据都没有时退回真实时钟。"""
    return data_anchor(catalog, cache) or dt.datetime.now().replace(microsecond=0)


# ============================================================================
# 安防告警
# ============================================================================
@security_router.get(
    "/alarms",
    response_model=AlarmListOut,
    summary="告警列表（含判据与证据）",
)
def list_alarms_endpoint(
    home_id: int | None = Query(None, description="只看这套房的告警（多套房下必传）"),
    status: str | None = Query(None, description="active / acknowledged / resolved / dismissed"),
    level: str | None = Query(None, description="info / warning / critical"),
    room_id: int | None = Query(None, description="只看某个房间"),
    from_: dt.datetime | None = Query(None, alias="from", description="起始时间，缺省 = 锚点往前 24 小时"),
    to_: dt.datetime | None = Query(None, alias="to", description="结束时间，缺省 = 数据锚点"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sync: bool = Query(True, description="是否在返回前重跑一次推导（幂等）"),
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
    rules: InsightsRules = Depends(get_insights_rules),
):
    anchor = _anchor(catalog, cache)
    window = resolve_window(from_, to_, default_hours=24.0, anchor=anchor)

    sync_out = AlarmSyncOut()
    if sync:
        ev = sync_event_alarms(db, rules, from_dt=window[0], to_dt=window[1])
        st = sync_state_alarms(db, rules, catalog, cache, anchor=anchor)
        sync_out = AlarmSyncOut(
            event_created=ev.created,
            event_skipped=ev.skipped,
            state_created=st.created,
            state_resolved=st.resolved,
            events_scanned=ev.scanned,
            events_unscored=ev.unscored,
        )

    data = list_alarms(
        db,
        anchor=anchor,
        home_id=home_id,
        status=status,
        room_id=room_id,
        level=level,
        from_dt=window[0],
        to_dt=window[1],
        limit=limit,
        offset=offset,
    )
    return AlarmListOut(**data, sync=sync_out)


@security_router.post(
    "/alarms/{alarm_id}/ack",
    response_model=AlarmAckOut,
    summary="人工确认告警（只记状态，不执行任何设备动作）",
)
def acknowledge_alarm_endpoint(
    alarm_id: int,
    body: AlarmAckIn | None = None,
    db: Session = Depends(get_db),
):
    try:
        alarm = acknowledge_alarm(
            db, alarm_id, acknowledged_by=(body.acknowledged_by if body else None)
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return AlarmAckOut(
        id=alarm.id,
        status=alarm.status,
        acknowledged_at=alarm.acknowledged_at,
        message=(
            f"已标记为「已知悉」。确认只代表你知道了这件事，不代表设备动作已经执行 —— "
            f"关燃气阀、锁门这类操作要走待确认通道，由你二次确认后才会下发。"
        ),
    )


# ============================================================================
# 照护观察
# ============================================================================
@care_router.get(
    "/observations",
    response_model=ObservationListOut,
    summary="照护观察时间轴（非摄像头数据，结论为待确认线索）",
)
def list_observations_endpoint(
    home_id: int | None = Query(None, description="只看这套房成员的观察（多套房下必传）"),
    member_id: int | None = Query(None, description="只看某位成员"),
    from_: dt.datetime | None = Query(None, alias="from", description="起始时间，缺省 = 锚点往前 24 小时"),
    to_: dt.datetime | None = Query(None, alias="to", description="结束时间，缺省 = 数据锚点"),
    limit: int = Query(200, ge=1, le=1000),
    sync: bool = Query(True, description="是否在返回前重跑一次推导（幂等）"),
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
    rules: InsightsRules = Depends(get_insights_rules),
):
    anchor = _anchor(catalog, cache)
    window = resolve_window(from_, to_, default_hours=24.0, anchor=anchor)

    coverage = []
    if sync:
        res = sync_observations(db, rules, catalog, anchor=anchor)
        coverage = [SubjectCoverageOut(**s.as_dict()) for s in res.subjects]

    data = list_observations(
        db, home_id=home_id, member_id=member_id,
        from_dt=window[0], to_dt=window[1], limit=limit,
    )
    return ObservationListOut(**data, coverage=coverage)


# ============================================================================
# 主动建议
# ============================================================================
@suggestions_router.get(
    "",
    response_model=SuggestionListOut,
    summary="主动建议（按打扰等级排序，含理由）",
)
def list_suggestions_endpoint(
    home_id: int | None = Query(None, description="只看这套房的建议（多套房下必传）"),
    level: str | None = Query(None, description="urgent / important / normal / silent"),
    status: str | None = Query(None, description="pending / shown / accepted / dismissed"),
    limit: int = Query(100, ge=1, le=500),
    sync: bool = Query(True, description="是否在返回前重跑一次推导（幂等）"),
    db: Session = Depends(get_db),
    catalog: DeviceCatalog = Depends(get_catalog),
    cache: LatestValueCache = Depends(get_cache),
    rules: InsightsRules = Depends(get_insights_rules),
    tariff: Tariff = Depends(get_tariff),
):
    if sync:
        anchor = _anchor(catalog, cache)
        sync_suggestions(
            db,
            rules,
            catalog,
            cache,
            anchor=anchor,
            tariff_prices={b.period: b.price for b in tariff.bands},
        )

    return SuggestionListOut(
        **list_suggestions(db, home_id=home_id, level=level, status=status, limit=limit)
    )


@suggestions_router.post(
    "/{suggestion_id}/dismiss",
    response_model=SuggestionActionOut,
    summary="忽略一条建议（12 小时内不再重复提）",
)
def dismiss_suggestion_endpoint(suggestion_id: int, db: Session = Depends(get_db)):
    try:
        s = dismiss_suggestion(db, suggestion_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return SuggestionActionOut(
        id=s.id,
        status=s.status,
        message="已忽略。同样的建议 12 小时内不会再出现；如果它确实该处理，仍然可以在告警页看到相关告警。",
    )


@suggestions_router.post(
    "/{suggestion_id}/accept",
    response_model=SuggestionActionOut,
    summary="采纳建议（只记状态，不下发指令）",
)
def accept_suggestion_endpoint(suggestion_id: int, db: Session = Depends(get_db)):
    try:
        s = accept_suggestion(db, suggestion_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return SuggestionActionOut(
        id=s.id,
        status=s.status,
        message="已记录为「采纳」。注意：这只表示你认可这条建议，系统没有执行任何设备动作 —— "
                "涉及门锁、燃气阀、总闸的操作请到设备页走待确认流程。",
    )
