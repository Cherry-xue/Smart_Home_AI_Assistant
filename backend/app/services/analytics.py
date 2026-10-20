"""历史读数与能耗分析。

两条硬约束（来自选型：规划书风险清单里"时序查询过重拖垮库"）
----------------------------------------------------------
1. **强制时间窗**：`from`/`to` 必填，且跨度不能超过上限 —— 否则一个手滑的查询
   就能把几百万行的时序表全拉出来。
2. **返回行数受控，但不再直接报错**：超过 `MAX_ROWS` 时**降采样**并显式标注
   （`downsampled=true` / `stride=N` / `raw_row_count=N`）。

   ⚠️ 这里改过行为，说明一下原因（2026-10-16）：
   原实现是"超限就直接 400"。但本端点的用途是**画曲线**，而 5 秒粒度的数据
   每小时就有 1400+ 行/设备 —— 也就是**只要库里有 3.5 小时以上数据，
   不传参数的默认调用（24h 窗）就必然 400**，端点等于不可用。
   而且前端画 2 万个点本来就没意义（既看不清也拖慢渲染），
   所以正确做法是"抽稀到能画得动的密度，并如实告诉调用方抽过稀"。
   真正该拒绝的是**连抽稀前的原始行数都过大**的情况（`HARD_ROW_CAP`），
   那时说明查询范围真的大得不合理。

金额一律用 `core/tariff.py` 的确定性代码算（规划书红线：**不让模型心算金额**）。
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.tariff import Tariff
from app.db.catalog import DeviceCatalog, DeviceInfo
from app.mqtt.cache import coerce_from_db

#: 单次查询允许的最大时间跨度（小时）。
MAX_WINDOW_HOURS = 24 * 7
#: 单次**返回**的最大点数。超过就按步长抽稀（不再是报错）。
MAX_ROWS = 5000
#: 抽稀前的**原始行数**硬上限。超过这个数说明时间窗/条件真的不合理，宁可拒绝，
#: 也不把几十万行读进内存再抽稀。
HARD_ROW_CAP = 200_000


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
    params: dict[str, Any] = {"did": dev.id, "f": from_dt, "t": to_dt, "lim": HARD_ROW_CAP + 1}
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
    raw_row_count = len(rows)

    if raw_row_count > HARD_ROW_CAP:
        raise QueryError(
            f"结果有 {raw_row_count} 行，超过 {HARD_ROW_CAP} 行的硬上限"
            "（这个量级连降采样都不合适）—— 请缩小时间窗或指定 metric 参数"
        )

    # ---- 第一遍：统计量按**原始行**算，不受抽稀影响 ----
    # ⚠️ 若拿抽稀后的点去算质量分布，比例会失真 —— 那是另一种误导。
    #    所以 count / quality_summary / stale_rows 全部来自原始行。
    extents: dict[str, dict[str, Any]] = {}
    quality_counter: dict[str, int] = {}
    stale_rows = 0
    for r in rows:
        cap = dev.cap(r.metric_code)
        ext = extents.setdefault(
            r.metric_code,
            {"unit": (cap.unit if cap and cap.unit else r.unit), "count": 0},
        )
        ext["count"] += 1
        quality_counter[r.quality] = quality_counter.get(r.quality, 0) + 1
        if r.is_stale:
            stale_rows += 1

    # ---- 抽稀：等步长取样，保住整体形状 ----
    stride = 1
    downsampled = False
    if raw_row_count > max_rows:
        stride = math.ceil(raw_row_count / max_rows)
        rows = rows[::stride]
        downsampled = True

    # ---- 第二遍：按指标分组装点 ----
    series: dict[str, dict[str, Any]] = {
        code: {"metric_code": code, "unit": ext["unit"], "points": [], "count": ext["count"]}
        for code, ext in extents.items()
    }

    for r in rows:
        cap = dev.cap(r.metric_code)
        value = coerce_from_db(r.value_num, r.value_text, cap)
        series[r.metric_code]["points"].append(
            {
                "observed_at": r.observed_at,
                "value": value,
                "quality": r.quality,
                "is_stale": bool(r.is_stale),
                "seq": r.seq,
            }
        )

    return {
        "device_id": dev.id,
        "device_code": dev.code,
        "device_name": dev.name,
        "room_code": dev.room_code,
        "high_risk": dev.high_risk,
        "from": from_dt,
        "to": to_dt,
        # ⚠️ row_count 是**原始行数**（数据库里真实有多少条），不是返回的点数。
        #    前端要判断"曲线是不是被抽稀了"，看 downsampled / stride。
        #    保留这个语义是为了让"每个指标的 count 之和 == row_count"始终成立。
        "row_count": raw_row_count,
        "returned_count": sum(len(b["points"]) for b in series.values()),
        "downsampled": downsampled,
        "stride": stride,
        "stale_rows": stale_rows,
        "quality_summary": quality_counter,
        "metrics": series,
        # 提示前端：画曲线时应当过滤掉质量不合格的点，而不是拿它们连线
        "note": (
            "points 里带 quality / is_stale，画曲线建议只用 quality='good' 且 is_stale=false 的点"
            + (
                f"；本次结果已降采样（原始 {raw_row_count} 行 → 每 {stride} 行取 1 点，"
                f"共返回 {sum(len(b['points']) for b in series.values())} 点），"
                "曲线形状保留，但不要用它统计精确极值"
                if downsampled
                else ""
            )
        ),
    }


# ---------------------------------------------------------------- 能耗汇总
def energy_summary(
    db: Session,
    catalog: DeviceCatalog,
    tariff: Tariff,
    *,
    from_dt: dt.datetime,
    to_dt: dt.datetime,
    home_id: int | None = None,
) -> dict[str, Any]:
    """账单口径的用能汇总：总量、分时段、分设备、逐小时。

    `home_id` 非空时只统计这套房的设备 —— 多套房下不传就会把别人的电费算进来。
    实现上用子查询圈设备 id，而不是 JOIN devices：`energy_readings` 上已经有
    `idx_energy_device_time`，子查询命中索引后代价与 JOIN 相当，但不用改三处 SQL 结构。
    """
    params = {"f": from_dt, "t": to_dt}
    home_clause = ""
    if home_id is not None:
        params["hid"] = home_id
        home_clause = " AND device_id IN (SELECT id FROM devices WHERE home_id = :hid)"
    price_of = {b.period: b.price for b in tariff.bands}

    # ---- 一次扫描，三个维度在 Python 里聚合 ----
    # 原来是分时段 / 分设备 / 逐小时各扫一遍 24 小时窗口（实测 15.7 万行 × 3 遍 ≈ 1.1 秒）。
    # 合并成一次扫描、分组粒度 (设备, 时段, 小时)：返回行数最多 设备数×4×24，
    # 比三次全表扫便宜得多。三个口径的累加逻辑都放在下面，金额仍按分时电价逐段算。
    rows = db.execute(
        text(
            f"""
            SELECT device_id,
                   tariff_period,
                   HOUR(observed_at) AS h,
                   COALESCE(SUM(energy_kwh), 0) AS kwh,
                   COALESCE(SUM(power_w), 0)    AS sum_w,
                   MAX(power_w)                 AS peak_w,
                   COUNT(*)                     AS rows_n
            FROM energy_readings
            WHERE observed_at >= :f AND observed_at <= :t{home_clause}
            GROUP BY device_id, tariff_period, HOUR(observed_at)
            """
        ),
        params,
    ).all()

    period_acc: dict[str | None, dict[str, float]] = {}
    device_acc: dict[int, dict[str, float]] = {}
    hour_acc: dict[int, dict[str, float]] = {}
    total_kwh = 0.0

    for r in rows:
        kwh = float(r.kwh or 0)
        sum_w = float(r.sum_w or 0)
        rows_n = int(r.rows_n or 0)
        peak = float(r.peak_w) if r.peak_w is not None else 0.0
        total_kwh += kwh

        # ① 分时段
        pacc = period_acc.setdefault(
            r.tariff_period, {"kwh": 0.0, "sum_w": 0.0, "rows": 0.0, "peak": 0.0}
        )
        pacc["kwh"] += kwh
        pacc["sum_w"] += sum_w
        pacc["rows"] += rows_n
        pacc["peak"] = max(pacc["peak"], peak)

        # ② 分设备：金额不能跨时段平均，必须按每段电价分别乘再加
        dacc = device_acc.setdefault(
            r.device_id, {"kwh": 0.0, "cost": 0.0, "sum_w": 0.0, "rows": 0.0, "peak": 0.0}
        )
        price = price_of.get(r.tariff_period) if r.tariff_period else None
        dacc["kwh"] += kwh
        dacc["cost"] += kwh * price if price is not None else 0.0
        dacc["sum_w"] += sum_w
        dacc["rows"] += rows_n
        dacc["peak"] = max(dacc["peak"], peak)

        # ③ 逐小时（与原实现一致：按 0~23 点合并，跨天也会并到一起）
        hacc = hour_acc.setdefault(int(r.h), {"kwh": 0.0, "sum_w": 0.0, "rows": 0.0})
        hacc["kwh"] += kwh
        hacc["sum_w"] += sum_w
        hacc["rows"] += rows_n

    # ---- 分时段 ----
    by_period: list[dict[str, Any]] = []
    total_cost = 0.0
    for period, acc in period_acc.items():
        kwh = acc["kwh"]
        price = price_of.get(period) if period else None
        cost = kwh * price if price is not None else None
        if cost:
            total_cost += cost
        by_period.append(
            {
                "period": period,
                "price": price,
                "kwh": round(kwh, 4),
                "cost": round(cost, 4) if cost is not None else None,
                "share": round(kwh / total_kwh, 4) if total_kwh else None,
                "avg_power_w": round(acc["sum_w"] / acc["rows"], 1) if acc["rows"] else None,
                "peak_power_w": round(acc["peak"], 1) if acc["peak"] else None,
                "rows": int(acc["rows"]),
            }
        )
    by_period.sort(
        key=lambda x: list(price_of).index(x["period"]) if x["period"] in price_of else 99
    )

    # ---- 分设备 ----
    # 设备元数据从目录取（39 条，内存），不再 JOIN devices/rooms：
    # 那次 JOIN 与聚合结果无关，只是"给 id 配个名字"，没必要放进慢查询里。
    meta = {d.id: d for d in catalog.all()}
    by_device: list[dict[str, Any]] = []
    for device_id, acc in device_acc.items():
        info = meta.get(device_id)
        if info is None:
            continue  # 设备已被删除，历史数据留着但不再展示
        by_device.append(
            {
                "device_id": device_id,
                "code": info.code,
                "name": info.name,
                "device_type": info.device_type,
                "room_name": info.room_name,
                "kwh": round(acc["kwh"], 4),
                "cost": round(acc["cost"], 4),
                "peak_power_w": round(acc["peak"], 1),
                "avg_power_w": round(acc["sum_w"] / acc["rows"], 1) if acc["rows"] else None,
                "share": round(acc["kwh"] / total_kwh, 4) if total_kwh else None,
            }
        )
    by_device.sort(key=lambda x: -x["kwh"])

    # ---- 逐小时 ----
    hourly = [
        {
            "hour": h,
            "kwh": round(acc["kwh"], 4),
            "avg_power_w": round(acc["sum_w"] / acc["rows"], 1) if acc["rows"] else None,
        }
        for h, acc in sorted(hour_acc.items())
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
