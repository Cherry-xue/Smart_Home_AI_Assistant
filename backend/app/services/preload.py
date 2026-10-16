"""启动时预热最新值缓存。

为什么需要
----------
服务刚起来时缓存是空的，此时问"客厅现在多少度"只能答"没有数据" ——
但库里明明有（上一次运行留下的）。预热让**"重启 = 数据立刻可见"**。

选取规则
--------
每个 (设备, 指标) 的**最后一条 `quality='good'` 且非 stale 的读数**，
并限制在最近 N 天内 —— 避免把很久以前的数据当成"最新值"（那会骗人）。

用窗口函数而不是 `GROUP BY + MAX` 子查询：后者在 `ONLY_FULL_GROUP_BY`
模式下要写一堆聚合，而且同一时间戳有多行（重复投递）时会返回重复行。MySQL 8 支持窗口函数。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db.catalog import DeviceCatalog
from app.mqtt.cache import LatestValueCache, coerce_from_db

PRELOAD_SQL = """
WITH ranked AS (
    SELECT dr.device_id, dr.metric_code, dr.value_num, dr.value_text,
           dr.unit, dr.observed_at,
           ROW_NUMBER() OVER (
               PARTITION BY dr.device_id, dr.metric_code
               ORDER BY dr.observed_at DESC, dr.id DESC
           ) AS rn
    FROM device_readings dr
    WHERE dr.quality = 'good' AND dr.is_stale = 0 AND dr.observed_at >= :since
)
SELECT d.code AS device_code, r.metric_code, r.value_num, r.value_text,
       r.unit, r.observed_at
FROM ranked r
JOIN devices d ON d.id = r.device_id
WHERE r.rn = 1 AND d.enabled = 1
"""


def preload_latest_values(
    engine: Engine,
    catalog: DeviceCatalog,
    cache: LatestValueCache,
    *,
    days: int = 2,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """把库里的最新可信读数灌进缓存。返回统计信息（便于日志与自检）。"""
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=days)

    with engine.connect() as conn:
        rows = conn.execute(text(PRELOAD_SQL), {"since": since}).all()

    grouped: dict[str, dict[str, tuple[object, dt.datetime]]] = {}
    units: dict[str, dict[str, str | None]] = {}
    for r in rows:
        dev = catalog.get(r.device_code)
        cap = dev.cap(r.metric_code) if dev is not None else None
        value = coerce_from_db(r.value_num, r.value_text, cap)
        grouped.setdefault(r.device_code, {})[r.metric_code] = (value, r.observed_at)
        units.setdefault(r.device_code, {})[r.metric_code] = (
            (cap.unit if cap and cap.unit else r.unit)
        )

    filled = 0
    for code, metrics in grouped.items():
        filled += cache.preload(code, metrics, units.get(code))

    return {
        "rows_scanned": len(rows),
        "devices": len(grouped),
        "values_filled": filled,
        "since": since,
        "oldest": min((m[1] for ms in grouped.values() for m in ms.values()), default=None),
    }
