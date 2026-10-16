"""历史读数与能耗分析。

两条硬约束（来自选型：规划书风险清单里"时序查询过重拖垮库"）
----------------------------------------------------------
1. **强制时间窗**：`from`/`to` 必填，且跨度不能超过上限 —— 否则一个手滑的查询
   就能把几百万行的时序表全拉出来。
2. **返回行数上限**：超了就**明确报错**，而不是默默截断 ——
   默默截断会让人以为"数据就这么少"，那是更糟的误导。

金额一律用 `core/tariff.py` 的确定性代码算（规划书红线：**不让模型心算金额**）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.tariff import Tariff
from app.db.catalog import DeviceCatalog, DeviceInfo
from app.mqtt.cache import coerce_from_db

#: 单次查询允许的最大时间跨度（小时）。首期不做降采样，靠这个上限兜住。
MAX_WINDOW_HOURS = 24 * 7
#: 单次返回的最大行数
MAX_ROWS = 5000


class QueryError(ValueError):
    """查询参数不合法（由 API 层转成 400）。"""


def resolve_window(
    from_dt: dt.datetime | None,
    to_dt: dt.datetime | None,
    *,
    default_hours: float = 24.0,
    anchor: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    """规整时间窗：缺省补默认值，并做合法性检查。

    ⚠️ 缺省基准用 `anchor`（数据时间锚点，见 `overview.data_anchor`）而不是"现在"：
    模拟器的时间轴可能跑到真实时钟之外，用"现在往前 24 小时"会一条都查不到，
    而实际上数据都在库里。真实部署时 anchor ≈ now，行为完全一致。
    """
    now = now or dt.datetime.now()
    base = anchor or now
    to_dt = to_dt or base
    from_dt = from_dt or (to_dt - dt.timedelta(hours=default_hours))

    if from_dt > to_dt:
        raise QueryError("from 不能晚于 to")

    span_hours = (to_dt - from_dt).total_seconds() / 3600.0
    if span_hours > MAX_WINDOW_HOURS:
        raise QueryError(
            f"时间跨度过大（{span_hours:.1f} 小时），单次查询上限是 {MAX_WINDOW_HOURS} 小时（{MAX_WINDOW_HOURS // 24} 天）"
        )
    return from_dt, to_dt


# ---------------------------------------------------------------- 设备历史读数
def device_readings(
    db: Session,
    dev: DeviceInfo,
    *,
    from_dt: dt.datetime,
    to_dt: dt.datetime,
    metrics: list[str] | None = None,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any]:
    """某台设备的历史读数（按指标分组，便于前端直接画曲线）。"""
    params: dict[str, Any] = {"did": dev.id, "f": from_dt, "t": to_dt, "lim": max_rows + 1}
    sql = """
        SELECT metric_code, value_num, value_text, unit, quality, is_stale,
               seq, observed_at
        FROM device_readings
        WHERE device_id = :did AND observed_at >= :f AND observed_at <= :t
    """
    if metrics:
        sql += " AND metric_code IN :metrics"
        params["metrics"] = tuple(metrics)
    sql += " ORDER BY observed_at ASC, id ASC LIMIT :lim"

    from sqlalchemy import bindparam

    stmt = text(sql)
    if metrics:
        stmt = stmt.bindparams(bindparam("metrics", expanding=True))

    rows = db.execute(stmt, params).all()
    if len(rows) > max_rows:
        raise QueryError(
            f"结果超过 {max_rows} 行，请缩小时间窗或指定 metric 参数（当前查询未返回任何数据，"
            f"以免给出不完整的结果）"
        )

    series: dict[str, dict[str, Any]] = {}
    quality_counter: dict[str, int] = {}
    stale_rows = 0

    for r in rows:
        cap = dev.cap(r.metric_code)
        value = coerce_from_db(r.value_num, r.value_text, cap)
        unit = (cap.unit if cap and cap.unit else r.unit)
        bucket = series.setdefault(
            r.metric_code,
            {"metric_code": r.metric_code, "unit": unit, "points": [], "count": 0},
        )
        bucket["points"].append(
            {
                "observed_at": r.observed_at,
                "value": value,
                "quality": r.quality,
                "is_stale": bool(r.is_stale),
                "seq": r.seq,
            }
        )
        bucket["count"] += 1
        quality_counter[r.quality] = quality_counter.get(r.quality, 0) + 1
        if r.is_stale:
            stale_rows += 1

    return {
        "device_id": dev.id,
        "device_code": dev.code,
        "device_name": dev.name,
        "room_code": dev.room_code,
        "high_risk": dev.high_risk,
        "from": from_dt,
        "to": to_dt,
        "row_count": len(rows),
        "stale_rows": stale_rows,
        "quality_summary": quality_counter,
        "metrics": series,
        # 提示前端：画曲线时应当过滤掉质量不合格的点，而不是拿它们连线
        "note": "points 里带 quality / is_stale，画曲线建议只用 quality='good' 且 is_stale=false 的点",
    }


# ---------------------------------------------------------------- 能耗汇总
def energy_summary(
    db: Session,
    catalog: DeviceCatalog,
    tariff: Tariff,
    *,
    from_dt: dt.datetime,
    to_dt: dt.datetime,
) -> dict[str, Any]:
    """账单口径的用能汇总：总量、分时段、分设备、逐小时。"""
    params = {"f": from_dt, "t": to_dt}
    price_of = {b.period: b.price for b in tariff.bands}

    # ---- 分时段 ----
    period_rows = db.execute(
        text(
            """
            SELECT tariff_period, COUNT(*) AS rows_n,
                   COALESCE(SUM(energy_kwh), 0) AS kwh,
                   AVG(power_w) AS avg_w, MAX(power_w) AS peak_w
            FROM energy_readings
            WHERE observed_at >= :f AND observed_at <= :t
            GROUP BY tariff_period
            """
        ),
        params,
    ).all()

    total_kwh = sum(float(r.kwh or 0) for r in period_rows)
    by_period: list[dict[str, Any]] = []
    total_cost = 0.0
    for r in period_rows:
        kwh = float(r.kwh or 0)
        price = price_of.get(r.tariff_period) if r.tariff_period else None
        cost = kwh * price if price is not None else None
        if cost:
            total_cost += cost
        by_period.append(
            {
                "period": r.tariff_period,
                "price": price,
                "kwh": round(kwh, 4),
                "cost": round(cost, 4) if cost is not None else None,
                "share": round(kwh / total_kwh, 4) if total_kwh else None,
                "avg_power_w": round(float(r.avg_w), 1) if r.avg_w is not None else None,
                "peak_power_w": round(float(r.peak_w), 1) if r.peak_w is not None else None,
                "rows": r.rows_n,
            }
        )
    by_period.sort(key=lambda x: list(price_of).index(x["period"]) if x["period"] in price_of else 99)

    # ---- 分设备（按 period 分组算成本，再在 Python 里聚合 —— 保证金额口径准确）----
    device_rows = db.execute(
        text(
            """
            SELECT d.id AS device_id, d.code, d.name, d.device_type,
                   r.name AS room_name, er.tariff_period,
                   COALESCE(SUM(er.energy_kwh), 0) AS kwh,
                   AVG(er.power_w) AS avg_w, MAX(er.power_w) AS peak_w
            FROM energy_readings er
            JOIN devices d ON d.id = er.device_id
            LEFT JOIN rooms r ON r.id = d.room_id
            WHERE er.observed_at >= :f AND er.observed_at <= :t
            GROUP BY d.id, d.code, d.name, d.device_type, r.name, er.tariff_period
            """
        ),
        params,
    ).all()

    agg: dict[int, dict[str, Any]] = {}
    for r in device_rows:
        item = agg.setdefault(
            r.device_id,
            {
                "device_id": r.device_id,
                "code": r.code,
                "name": r.name,
                "device_type": r.device_type,
                "room_name": r.room_name,
                "kwh": 0.0,
                "cost": 0.0,
                "peak_power_w": 0.0,
                "avg_power_w": None,
            },
        )
        kwh = float(r.kwh or 0)
        price = price_of.get(r.tariff_period) if r.tariff_period else None
        item["kwh"] += kwh
        item["cost"] += kwh * price if price is not None else 0.0
        if r.peak_w is not None:
            item["peak_power_w"] = max(item["peak_power_w"], float(r.peak_w))
        if r.avg_w is not None:
            item["avg_power_w"] = round(float(r.avg_w), 1)

    by_device = sorted(agg.values(), key=lambda x: -x["kwh"])
    for item in by_device:
        item["kwh"] = round(item["kwh"], 4)
        item["cost"] = round(item["cost"], 4)
        item["share"] = round(item["kwh"] / total_kwh, 4) if total_kwh else None

    # ---- 逐小时 ----
    hourly_rows = db.execute(
        text(
            """
            SELECT HOUR(observed_at) AS h,
                   COALESCE(SUM(energy_kwh), 0) AS kwh,
                   AVG(power_w) AS avg_w
            FROM energy_readings
            WHERE observed_at >= :f AND observed_at <= :t
            GROUP BY HOUR(observed_at)
            ORDER BY h
            """
        ),
        params,
    ).all()
    hourly = [
        {
            "hour": int(r.h),
            "kwh": round(float(r.kwh or 0), 4),
            "avg_power_w": round(float(r.avg_w), 1) if r.avg_w is not None else None,
        }
        for r in hourly_rows
    ]

    return {
        "from": from_dt,
        "to": to_dt,
        "currency": tariff.currency,
        "unit": tariff.unit,
        "total_kwh": round(total_kwh, 4),
        "total_cost": round(total_cost, 4),
        "by_period": by_period,
        "by_device": by_device,
        "hourly": hourly,
        "observations": _observations(by_period, by_device, total_kwh, total_cost),
        "note": "金额由确定性代码按分时电价计算，未经模型估算",
    }


def _observations(
    by_period: list[dict[str, Any]],
    by_device: list[dict[str, Any]],
    total_kwh: float,
    total_cost: float,
) -> list[str]:
    """把数据里**直接算出**的事实说清楚（只陈述，不给建议 —— 建议属能耗 Agent）。"""
    notes: list[str] = []
    if total_kwh <= 0:
        return ["所选时间窗内没有可用的能耗数据"]

    with_cost = [p for p in by_period if p["cost"] is not None]
    if with_cost:
        max_kwh = max(by_period, key=lambda p: p["kwh"])
        max_cost = max(with_cost, key=lambda p: p["cost"])
        notes.append(
            f"用电最多的是 {max_kwh['period']} 时段：{max_kwh['kwh']} kWh（占 {max_kwh['share'] * 100:.1f}%）"
        )
        notes.append(
            f"花费最多的是 {max_cost['period']} 时段：¥{max_cost['cost']}（占 ¥{round(total_cost, 4)} 的 "
            f"{max_cost['cost'] / total_cost * 100:.1f}%）"
        )
        if max_kwh["period"] != max_cost["period"]:
            notes.append(
                f"注意：{max_kwh['period']} 时段用电最多、但 {max_cost['period']} 时段花钱最多 —— "
                f"两者不一致说明单价差异比用电量差异影响更大"
            )
    if by_device:
        top = by_device[0]
        notes.append(
            f"耗电最多的设备是 {top['name']}（{top['kwh']} kWh，占 {top['share'] * 100:.1f}%），"
            f"峰值功率 {top['peak_power_w']} W"
        )
    return notes
