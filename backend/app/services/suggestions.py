"""主动建议：把"系统认为值得提一句的事"整理成可解释、可关闭的条目。

三条设计约束
------------
1. **每条建议都要带理由**（`reason_json`）。没有理由的推送等于噪音，
   用户无法判断该不该听 —— 这条是需求分析里"可解释、可关闭"要求的落点。

2. **打扰等级要能被免打扰配置压制**。夜间 22:00 之后，"建议把洗衣机挪到谷电时段"
   这种 normal 级建议不该弹出来。压制不是丢弃：条目仍在列表里，只是标成 silent
   并写明"因为处在免打扰时段而降级" —— 用户白天回看时还能看到。

3. **建议不等于执行**。哪怕用户点了"采纳"，这里也**只记录状态**，
   绝不下发设备指令。凡是涉及燃气阀、门锁、总闸的建议，真正动作必须走
   高风险待确认通道（那是另一条链路，需要人工二次确认）。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.insights_rules import InsightsRules, SuggestionRule
from app.db.catalog import DeviceCatalog
from app.mqtt.cache import LatestValueCache
from app.models.orm import Suggestion

log = logging.getLogger(__name__)

DISCLAIMER = "建议由规则从当前状态推导，理由见 reason；采纳只记录意向，不会自动下发设备指令"

#: 打扰等级从高到低
LEVEL_ORDER = ("urgent", "important", "normal", "silent")
#: 建议"还在桌上"的状态（用户还没处理）
OPEN_STATUSES = ("pending", "shown", "accepted")
#: 被忽略后多久内不再重复提同一条
DISMISS_COOLDOWN_HOURS = 12.0


@dataclasses.dataclass
class SyncResult:
    created: int = 0
    skipped: int = 0
    downgraded: int = 0

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


# ============================================================================
# 免打扰
# ============================================================================
def _as_time(v: Any) -> dt.time:
    """pymysql 把 TIME 列读成 timedelta，SQLAlchemy 可能给 time —— 两种都接住。"""
    if isinstance(v, dt.time):
        return v
    if isinstance(v, dt.timedelta):
        total = int(v.total_seconds()) % 86400
        return dt.time(total // 3600, (total % 3600) // 60, total % 60)
    return dt.time(0, 0)


def _in_window(t: dt.time, start: dt.time, end: dt.time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _quiet_levels(db: Session, moment: dt.datetime, member_id: int | None) -> dict[str, str]:
    """当前时刻处于哪些等级的免打扰区间。返回 {等级: "HH:MM-HH:MM"}。"""
    rows = db.execute(
        text(
            """
            SELECT level, quiet_start, quiet_end
            FROM disturbance_rules
            WHERE enabled = 1 AND (member_id IS NULL OR member_id = :mid)
            """
        ),
        {"mid": member_id if member_id is not None else -1},
    ).all()

    t = moment.time()
    out: dict[str, str] = {}
    for r in rows:
        s, e = _as_time(r.quiet_start), _as_time(r.quiet_end)
        if _in_window(t, s, e):
            out[str(r.level)] = f"{s:%H:%M}-{e:%H:%M}"
    return out


def _apply_quiet(level: str, quiet: dict[str, str]) -> tuple[str, str | None]:
    """按免打扰配置降级。返回 (最终等级, 降级说明)。"""
    window = quiet.get(level)
    if window is None:
        return level, None
    note = f"当前处于「{level}」级免打扰时段（{window}），已降级为 silent，白天可在建议列表里回看"
    return "silent", note


# ============================================================================
# 幂等写入
# ============================================================================
def _find_open(db: Session, dedupe_key: str) -> Suggestion | None:
    """同一条建议是否还"在桌上"。

    去重键存在 `reason_json.dedupe` 里 —— 库里没有专门的 dedupe 列，
    而 `app_rw` 没有 DDL 权限（DDL 只能在虚拟机手工执行，这是既定约束），
    所以用 JSON 字段承载。MySQL 8 支持对 JSON 建函数索引，量大了可以再加。
    """
    return db.execute(
        select(Suggestion)
        .where(
            Suggestion.reason_json["dedupe"].as_string() == dedupe_key,
            Suggestion.status.in_(OPEN_STATUSES),
        )
        .order_by(Suggestion.id.desc())
        .limit(1)
    ).scalars().first()


def _recently_dismissed(db: Session, dedupe_key: str, moment: dt.datetime) -> bool:
    since = moment - dt.timedelta(hours=DISMISS_COOLDOWN_HOURS)
    row = db.execute(
        text(
            """
            SELECT 1 FROM suggestions
            WHERE JSON_UNQUOTE(JSON_EXTRACT(reason_json, '$.dedupe')) = :k
              AND status = 'dismissed'
              AND dismissed_at IS NOT NULL
              AND dismissed_at >= :since
            LIMIT 1
            """
        ),
        {"k": dedupe_key, "since": since},
    ).first()
    return row is not None


def _add(
    db: Session,
    *,
    home_id: int,
    member_id: int | None,
    content: str,
    level: str,
    reason: dict[str, Any],
    dedupe_key: str,
    quiet: dict[str, str],
) -> tuple[bool, bool]:
    """返回 (是否新建, 是否发生降级)。"""
    if _find_open(db, dedupe_key) is not None:
        return False, False

    final_level, note = _apply_quiet(level, quiet)
    if note:
        reason = {**reason, "downgraded_from": level, "downgrade_note": note}

    db.add(
        Suggestion(
            home_id=home_id,
            member_id=member_id,
            content=content[:512],
            reason_json={**reason, "dedupe": dedupe_key},
            disturbance_level=final_level,
            status="pending",
        )
    )
    return True, note is not None


# ============================================================================
# 一、从告警派生（urgent / important）
# ============================================================================
def _from_alarms(
    db: Session, rules: InsightsRules, *, home_id: int, anchor: dt.datetime,
    quiet: dict[str, str],
) -> SyncResult:
    res = SyncResult()
    # alarm_code → 打扰等级：从事件规则反查（同一条告警可能来自不同事件码，取最高）
    level_of: dict[str, str] = {}
    for rule in rules.event_rules.values():
        prev = level_of.get(rule.alarm_code)
        if prev is None or LEVEL_ORDER.index(rule.suggestion_level) < LEVEL_ORDER.index(prev):
            level_of[rule.alarm_code] = rule.suggestion_level

    rows = db.execute(
        text(
            """
            SELECT a.id, a.alarm_code, a.level, a.title, a.detail, a.created_at,
                   a.device_id, d.name AS device_name, r.name AS room_name
            FROM alarms a
            LEFT JOIN devices d ON d.id = a.device_id
            LEFT JOIN rooms r ON r.id = a.room_id
            WHERE a.status IN ('active', 'acknowledged')
              AND a.created_at >= :f
            ORDER BY a.created_at DESC
            LIMIT 100
            """
        ),
        {"f": anchor - dt.timedelta(hours=24)},
    ).all()

    for r in rows:
        lvl = level_of.get(r.alarm_code)
        if lvl is None:
            continue                     # 运行态告警（失联之类）不进"建议"，那不是建议而是告警本身
        action = ""
        if r.detail:
            for line in str(r.detail).splitlines():
                if line.startswith("建议处置："):
                    action = line[len("建议处置："):]
                    break
        content = f"关于「{r.title}」：{action}" if action else f"需要处理：「{r.title}」"
        key = f"alarm:{r.id}"
        if _recently_dismissed(db, key, anchor):
            res.skipped += 1
            continue
        created, downgraded = _add(
            db,
            home_id=home_id,
            member_id=None,
            content=content,
            level=lvl,
            reason={
                "rule": "alarm_followup",
                "alarm_id": int(r.id),
                "alarm_code": r.alarm_code,
                "alarm_level": r.level,
                "alarm_at": r.created_at.isoformat() if r.created_at else None,
                "device": r.device_name,
                "room": r.room_name,
                "note": "建议来自一条活跃告警，告警详情里能看到完整判据",
            },
            dedupe_key=key,
            quiet=quiet,
        )
        res.created += int(created)
        res.skipped += int(not created)
        res.downgraded += int(downgraded)
    return res


# ============================================================================
# 二、从能耗派生（normal）
# ============================================================================
def _energy_share(
    db: Session, from_dt: dt.datetime, to_dt: dt.datetime
) -> tuple[float, float, dict[str, float]]:
    rows = db.execute(
        text(
            """
            SELECT tariff_period, SUM(energy_kwh) AS kwh
            FROM energy_readings
            WHERE observed_at >= :f AND observed_at <= :t
            GROUP BY tariff_period
            """
        ),
        {"f": from_dt, "t": to_dt},
    ).all()
    kwh = {str(r.tariff_period or "unknown"): float(r.kwh or 0) for r in rows}
    total = sum(kwh.values())
    peak = kwh.get("peak", 0.0)
    return peak, total, kwh


def _from_energy(
    db: Session, rule: SuggestionRule, *, home_id: int, anchor: dt.datetime,
    tariff_prices: dict[str, float], quiet: dict[str, str],
) -> SyncResult:
    res = SyncResult()
    hours = float(rule.params.get("window_hours", 24))
    threshold = float(rule.params.get("peak_share_threshold", 0.45))
    from_dt = anchor - dt.timedelta(hours=hours)

    peak, total, kwh = _energy_share(db, from_dt, anchor)
    if total <= 0:
        return res

    share = peak / total
    if share < threshold:
        return res

    key = "energy:peak_share"
    if _recently_dismissed(db, key, anchor):
        res.skipped += 1
        return res

    valley_price = tariff_prices.get("valley")
    try:
        content = str(rule.params.get("content_tpl", "")).format(
            hours=f"{hours:.0f}",
            peak_pct=f"{share:.0%}",
            peak_kwh=f"{peak:.2f}",
            total_kwh=f"{total:.2f}",
            valley_price=f"{valley_price:.2f}" if valley_price is not None else "未知",
        )
    except (KeyError, ValueError):
        content = f"最近 {hours:.0f} 小时峰时段用电占比 {share:.0%}，建议把可延迟的负载挪到谷时段"
    try:
        criteria = str(rule.params.get("criteria_tpl", "")).format(
            hours=f"{hours:.0f}", peak_pct=f"{share:.0%}",
            peak_kwh=f"{peak:.2f}", total_kwh=f"{total:.2f}",
        )
    except (KeyError, ValueError):
        criteria = f"峰段 {peak:.2f} kWh / 总计 {total:.2f} kWh = {share:.0%}"

    created, downgraded = _add(
        db,
        home_id=home_id,
        member_id=None,
        content=content,
        level=rule.level,
        reason={
            "rule": rule.name,
            "criteria": criteria,
            "peak_kwh": round(peak, 4),
            "total_kwh": round(total, 4),
            "peak_share": round(share, 4),
            "threshold": threshold,
            "periods": {k: round(v, 4) for k, v in kwh.items()},
            "window": {"from": from_dt.isoformat(), "to": anchor.isoformat()},
            "note": "金额与占比由 core/tariff.py 按分时电价确定性计算，不是模型估的",
        },
        dedupe_key=key,
        quiet=quiet,
    )
    res.created += int(created)
    res.skipped += int(not created)
    res.downgraded += int(downgraded)
    return res


# ============================================================================
# 三、从设备状态派生（normal）
# ============================================================================
def _room_has_presence_sensor(catalog: DeviceCatalog, room_code: str) -> bool:
    return any(d.device_type == "mmwave_radar" for d in catalog.in_room(room_code))


def _from_device_state(
    db: Session, rule: SuggestionRule, *, home_id: int, catalog: DeviceCatalog,
    cache: LatestValueCache, anchor: dt.datetime, quiet: dict[str, str],
) -> SyncResult:
    res = SyncResult()
    idle_min = float(rule.params.get("idle_minutes", 60))
    since = anchor - dt.timedelta(minutes=idle_min)

    for dev in catalog.all():
        if dev.device_type not in ("light", "lamp"):
            continue
        if cache.value_of(dev.code, "power_on") is not True:
            continue
        # 没有在场传感器的房间，无法判断"有没有人" —— 不猜
        if not _room_has_presence_sensor(catalog, dev.room_code):
            continue

        presence_device_ids = [
            d.id for d in catalog.in_room(dev.room_code) if d.device_type == "mmwave_radar"
        ]
        marks = ", ".join(f":i{i}" for i in range(len(presence_device_ids)))
        p_params = {f"i{i}": v for i, v in enumerate(presence_device_ids)}
        p_params["f"] = since
        seen = db.execute(
            text(
                f"""
                SELECT COUNT(*) FROM device_readings
                WHERE device_id IN ({marks}) AND metric_code = 'presence'
                  AND value_num = 1 AND observed_at >= :f
                """
            ),
            p_params,
        ).scalar() or 0
        if seen:
            continue                        # 期间有人，不算浪费

        # 确认它在这段时间里一直是开着的：窗口内没出现过 power_on=0
        off = db.execute(
            text(
                """
                SELECT COUNT(*) FROM device_readings
                WHERE device_id = :d AND metric_code = 'power_on'
                  AND value_num = 0 AND observed_at >= :f
                """
            ),
            {"d": dev.id, "f": since},
        ).scalar() or 0
        if off:
            continue                        # 中途关过又开，说明是最近才开的，别急着建议

        key = f"light_idle:{dev.id}"
        if _recently_dismissed(db, key, anchor):
            res.skipped += 1
            continue
        try:
            content = str(rule.params.get("content_tpl", "")).format(
                device=dev.name, room=dev.room_name, minutes=f"{idle_min:.0f}"
            )
        except (KeyError, ValueError):
            content = f"「{dev.name}」亮了 {idle_min:.0f} 分钟，但{dev.room_name}一直没人，可以关掉"

        created, downgraded = _add(
            db,
            home_id=home_id,
            member_id=None,
            content=content,
            level=rule.level,
            reason={
                "rule": rule.name,
                "criteria": f"power_on=true 且窗口内无 power_on=false 记录；"
                            f"同房间毫米波同期无 presence=true（{idle_min:.0f} 分钟）",
                "device_code": dev.code,
                "device_name": dev.name,
                "room": dev.room_name,
                "idle_minutes": idle_min,
                "note": "关灯属于低风险动作，但仍需要你确认；系统不会自己关",
            },
            dedupe_key=key,
            quiet=quiet,
        )
        res.created += int(created)
        res.skipped += int(not created)
        res.downgraded += int(downgraded)
    return res


# ============================================================================
# 对外
# ============================================================================
def sync_suggestions(
    db: Session,
    rules: InsightsRules,
    catalog: DeviceCatalog,
    cache: LatestValueCache,
    *,
    anchor: dt.datetime,
    tariff_prices: dict[str, float] | None = None,
) -> SyncResult:
    homes = catalog.home_ids()
    home_id = homes[0] if homes else 1
    quiet = _quiet_levels(db, anchor, None)

    total = SyncResult()
    parts = [_from_alarms(db, rules, home_id=home_id, anchor=anchor, quiet=quiet)]

    energy_rule = rules.suggestion_rules.get("peak_share_high")
    if energy_rule is not None and energy_rule.enabled:
        parts.append(
            _from_energy(
                db, energy_rule, home_id=home_id, anchor=anchor,
                tariff_prices=tariff_prices or {}, quiet=quiet,
            )
        )

    light_rule = rules.suggestion_rules.get("always_on_light")
    if light_rule is not None and light_rule.enabled:
        parts.append(
            _from_device_state(
                db, light_rule, home_id=home_id, catalog=catalog,
                cache=cache, anchor=anchor, quiet=quiet,
            )
        )

    for p in parts:
        total.created += p.created
        total.skipped += p.skipped
        total.downgraded += p.downgraded

    if total.created:
        db.commit()
        log.info("主动建议：新增 %d 条（其中 %d 条因免打扰降级）", total.created, total.downgraded)
    return total


def list_suggestions(
    db: Session,
    *,
    home_id: int | None = None,
    level: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    where = ["1=1"]
    params: dict[str, Any] = {"lim": limit}
    if home_id is not None:
        where.append("s.home_id = :hid")
        params["hid"] = home_id
    if level:
        where.append("s.disturbance_level = :lvl")
        params["lvl"] = level
    if status:
        where.append("s.status = :st")
        params["st"] = status
    clause = " AND ".join(where)

    rows = db.execute(
        text(
            f"""
            SELECT s.id, s.home_id, s.member_id, s.content, s.reason_json,
                   s.disturbance_level, s.status, s.dismissed_at, s.created_at,
                   m.nickname
            FROM suggestions s
            LEFT JOIN members m ON m.id = s.member_id
            WHERE {clause}
            ORDER BY FIELD(s.disturbance_level,'urgent','important','normal','silent'),
                     s.created_at DESC, s.id DESC
            LIMIT :lim
            """
        ),
        params,
    ).all()

    by_level: dict[str, int] = {}
    for lv, n in db.execute(
        text("SELECT disturbance_level, COUNT(*) FROM suggestions GROUP BY disturbance_level")
    ).all():
        by_level[str(lv)] = int(n)

    items = []
    for r in rows:
        reason = r.reason_json
        if isinstance(reason, str):
            try:
                reason = json.loads(reason)
            except json.JSONDecodeError:
                reason = None
        items.append(
            {
                "id": r.id,
                "member_id": r.member_id,
                "nickname": r.nickname,
                "content": r.content,
                "reason": reason or {},
                "disturbance_level": r.disturbance_level,
                "status": r.status,
                "is_open": r.status in OPEN_STATUSES,
                "quiet_downgraded": bool((reason or {}).get("downgraded_from")),
                "created_at": r.created_at,
                "dismissed_at": r.dismissed_at,
            }
        )

    return {
        "items": items,
        "total": len(items),
        "by_level": by_level,
        "order": list(LEVEL_ORDER),
        "disclaimer": DISCLAIMER,
    }


def dismiss_suggestion(db: Session, suggestion_id: int) -> Suggestion:
    s = db.get(Suggestion, suggestion_id)
    if s is None:
        raise LookupError(f"建议 {suggestion_id} 不存在")
    s.status = "dismissed"
    s.dismissed_at = dt.datetime.now()
    db.commit()
    db.refresh(s)
    return s


def accept_suggestion(db: Session, suggestion_id: int) -> Suggestion:
    """记录"用户采纳了这条建议"。

    ⚠️ 只记状态，**不执行任何设备动作**。建议里可能包含"关闭燃气阀"这种
    高风险动作，它必须单独走待确认通道 —— 点一下"采纳"不等于授权执行。
    """
    s = db.get(Suggestion, suggestion_id)
    if s is None:
        raise LookupError(f"建议 {suggestion_id} 不存在")
    s.status = "accepted"
    db.commit()
    db.refresh(s)
    return s
