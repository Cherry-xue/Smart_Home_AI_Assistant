"""`GET /api/health` —— 系统状态。

这个接口不是"返回 ok 就完事"，它要能一眼回答四个问题：
1. **库通不通**（含往返耗时 —— 远程库慢的时候，这一项最先体现出来）；
2. **MQTT 通不通**、订阅线程活着没、累计收了多少条（`data_mode=mqtt` 时）；
3. **最新值缓存里有多少台设备**、哪些设备已经很久没上报了（设备离线是很要紧的信号）；
4. 数据采集侧的质量计数（拒收 / 重复 / stale / 越界 / retained 快照）。

⚠️ `status` 会如实反映"MQTT 没连上"（`degraded`）。broker 不是 Windows 服务、
每次开机要手动起 —— 所以开发期看到 degraded 是正常的，看 `mqtt.error` 就知道原因。
**不粉饰**：health 一旦开始骗人，它就失去了意义。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_cache, get_catalog, get_service, get_worker
from app.config.settings import get_settings
from app.core.tariff import get_tariff
from app.db.session import get_db, ping
from app.schemas.system import (
    CacheHealth,
    DatabaseHealth,
    HealthOut,
    IngestHealth,
    MqttHealth,
)
from app.services.overview import data_anchor

router = APIRouter(tags=["system"])

#: 多久没上报算离线（与 overview.DEFAULT_OFFLINE_AFTER_SEC 保持一致）
OFFLINE_AFTER_SEC = 600.0
#: health 里最多列出几个离线设备（避免设备多了以后响应膨胀）
MAX_OFFLINE_LISTED = 10


@router.get("/health", response_model=HealthOut, summary="系统状态检查")
async def health(
    request: Request,
    db: Session = Depends(get_db),  # noqa: ARG001 —— 用于确保数据库会话可用
    catalog=Depends(get_catalog),
    cache=Depends(get_cache),
    service=Depends(get_service),
    worker=Depends(get_worker),
):
    settings = get_settings()

    # ---------------- 数据库 ----------------
    # ping() 是同步的，必须丢线程池 —— 直接在 async 端点里调用会阻塞整个事件循环
    started = time.perf_counter()
    try:
        info = await asyncio.to_thread(ping)
        database = DatabaseHealth(
            ok=True,
            version=info.get("version"),
            charset=info.get("charset"),
            current_user=info.get("current_user"),
            response_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    except Exception as e:  # noqa: BLE001
        database = DatabaseHealth(ok=False, error=f"{type(e).__name__}: {e}")

    # ---------------- MQTT 与采集统计 ----------------
    if service is not None:
        wstat = worker.status() if worker is not None else {}
        sstat = service.service_stats
        istat = service.ingestor.stats
        mqtt = MqttHealth(
            enabled=True,
            connected=bool(wstat.get("connected", sstat.connected)),
            thread_alive=bool(wstat.get("thread_alive", False)),
            uptime_sec=float(wstat.get("uptime_sec", 0.0)),
            sessions=sstat.sessions,
            reconnects=sstat.reconnects,
            messages_seen=sstat.messages_seen,
            last_message_at=sstat.last_message_at,
            error=wstat.get("error"),
        )
        allowed = IngestHealth.model_fields.keys()
        ingest = IngestHealth(**{k: v for k, v in istat.as_dict().items() if k in allowed})
    else:
        mqtt = MqttHealth(enabled=False)
        ingest = IngestHealth()

    # ---------------- 缓存与离线设备 ----------------
    now = dt.datetime.now()
    all_codes = [d.code for d in catalog.all()]
    offline = cache.devices_without_update(now, OFFLINE_AFTER_SEC, known_devices=all_codes)
    cache_stats = cache.stats()
    cache_health = CacheHealth(
        devices=cache_stats["devices"],
        values=cache_stats["values"],
        preloaded=int(getattr(request.app.state, "cache_preloaded", 0)),
        offline_count=len(offline),
        offline_devices=offline[:MAX_OFFLINE_LISTED],
    )

    # 链路断开 → 在线数必须是 0。
    # 与 `/api/devices`、房间卡片同一口径：链路断了就无从判断设备状态，
    # 保留"32 台在线"会与"链路断开"的红框自相矛盾。
    link_down = settings.data_mode == "mqtt" and service is not None and not mqtt.connected

    ok = database.ok and (not mqtt.enabled or mqtt.connected)
    return HealthOut(
        status="ok" if ok else "degraded",
        app_env=settings.app_env,
        data_mode=settings.data_mode,
        timezone=settings.timezone,
        generated_at=now,
        data_anchor=data_anchor(catalog, cache),
        database=database,
        mqtt=mqtt,
        ingest=ingest,
        cache=cache_health,
        devices={
            "total": len(catalog),
            "rooms": len(catalog.rooms),
            "high_risk": len(catalog.high_risk()),
            "online": 0 if link_down else len(all_codes) - len(offline),
        },
        tariff=get_tariff().describe(),
        high_risk_auto_exec=settings.is_high_risk_auto_exec_enabled(),
    )
