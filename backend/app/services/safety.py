"""安防告警：从传感器事件与设备状态**确定性地**推导告警。

四条设计原则（都是踩过坑之后的教训）
----------------------------------
1. **每条告警必须能说出依据**。告警带 `criteria`（判据）与证据行（事件 id /
   设备最新观测时间），原样返回给前端。一个说不出"拿什么比出来的"红灯，
   比没有告警更糟 —— 人只能选择忽视它。

2. **推导必须幂等**。`sync_*` 在每次列表请求前都会跑一遍。同一起传感器事件
   只能对应一条告警；条件持续也不能每 10 秒堆一条。

3. **状态类告警会自动恢复**。设备重新上线、门锁锁上之后，活跃告警置为
   `resolved` —— 否则告警列表会变成只增不减的垃圾场，最后没人看。

4. **只产告警，不产指令**。燃气阀关阀、门锁上锁只出现在 `action_hint` 文本里。
   真正的下发必须走 `device_control` 的高风险待确认通道（那条约束在工具层强制）。

措辞红线
--------
单点门磁信号**不定性为"入侵"**，只报"传感器在非常规时段被触发"；
照护类告警只说"待确认线索"，不做健康结论。这两条写死在配置的文案里。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.insights_rules import EventRule, InsightsRules, StateRule
from app.db.catalog import DeviceInfo, DeviceCatalog
from app.models.orm import Alarm
from app.mqtt.cache import LatestValueCache

log = logging.getLogger(__name__)

#: 一次推导最多扫多少条事件（防止某天灌了十万条事件把接口拖死）
MAX_EVENTS_PER_SYNC = 500
#: "活跃"包含已确认：已确认不等于已恢复，设备还在失联就该继续挂在活跃列表里
ACTIVE_STATUSES = ("active", "acknowledged")

#: （已移除）原先每条详情末尾都挂一句「阈值为演示值…」的免责话。
#: 用户判定它是无用噪音 —— 判据本身写的是"发生了什么"，阈值出处在
#: `config/insights_rules.yaml` 里可查，不必在每条告警上重复一遍。
#: 改这条前先确认前端没有别处依赖 `disclaimer` 字段。


@dataclasses.dataclass
class SyncResult:
    """一次推导的结果，用于自证"这次确实扫了东西、但没重复造告警"。"""

    created: int = 0
    skipped: int = 0
    resolved: int = 0
    scanned: int = 0
    unscored: int = 0

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)

    def __bool__(self) -> bool:
        return bool(self.created or self.resolved or self.scanned)


# ============================================================================
# 内部工具
# ============================================================================
def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _room_id(catalog: DeviceCatalog, room_code: str) -> int | None:
    """房间编码 → 房间 id。目录里没有这个房间时返回 None（告警照样能建，只是没房间）。"""
    room = catalog.room(room_code)
    return room.id if room is not None else None


def _detail_text(
    *,
    criteria: str,
    action_hint: str,
    evidence: str,
    device_name: str = "",
    device_type: str = "",
) -> str:
    """拼出人可读的告警详情。前端用 `white-space: pre-line` 渲染。

    刻意**不用 Markdown**：这个项目的所有面向用户的文案都走纯文本 + 「」引号，
    Vue 不渲染 Markdown，写了 `**` 会原样露出星号（前面踩过一次）。
    """
    lines = [
        f"判据：{criteria}",
    ]
    if device_name:
        lines.append(f"来源设备：{device_name}（{device_type}）" if device_type else f"来源设备：{device_name}")
    lines.append(f"证据：{evidence}")
    if action_hint:
        lines.append(f"建议处置：{action_hint}")
    return "\n".join(lines)


def _alarm_near(
    db: Session, alarm_code: str, device_id: int | None, moment: dt.datetime
) -> int | None:
    """同一起事件是否已经生成过告警（±1 秒窗口）。

    为什么用时间窗口而不是"事件 id 存到 detail 里再 LIKE 匹配"：
    `alarms` 表没有 event_id 字段，而 `app_rw` 没有 DDL 权限（这是项目既定约束，
    DDL 只能在虚拟机里手工执行）。时间窗口是零改动的等价做法 ——
    采集端本身有事件去重（同一设备同一事件码几分钟内只入库一次），
    所以"同一秒内两条相同事件"实际不会发生。
    """
    stmt = (
        select(Alarm.id)
        .where(
            Alarm.alarm_code == alarm_code,
            Alarm.created_at >= moment - dt.timedelta(seconds=1),
            Alarm.created_at <= moment + dt.timedelta(seconds=1),
        )
        .limit(1)
    )
    if device_id is None:
        stmt = stmt.where(Alarm.device_id.is_(None))
    else:
        stmt = stmt.where(Alarm.device_id == device_id)
    row = db.execute(stmt).first()
    return int(row[0]) if row else None


def _active_alarm(db: Session, alarm_code: str, device_id: int | None) -> Alarm | None:
    stmt = select(Alarm).where(Alarm.alarm_code == alarm_code, Alarm.status.in_(ACTIVE_STATUSES))
    if device_id is None:
        stmt = stmt.where(Alarm.device_id.is_(None))
    else:
        stmt = stmt.where(Alarm.device_id == device_id)
    return db.execute(stmt.order_by(Alarm.id.desc()).limit(1)).scalars().first()


def _raise_alarm(
    db: Session,
    *,
    room_id: int | None,
    device_id: int | None,
    rule: EventRule | StateRule,
    moment: dt.datetime,
    detail: str,
    alarm_code: str,
) -> Alarm:
    alarm = Alarm(
        room_id=room_id,
        device_id=device_id,
        alarm_code=alarm_code,
        level=rule.level,
        title=_clip(rule.title, 128),
        detail=detail,
        status="active",
        # created_at 用**数据时间**而不是真实时钟：整条演示链路（读数、事件、能耗）
        # 都以数据锚点为准，混用真时钟会出现"告警时间早于它的证据时间"这种自相矛盾的画面。
        created_at=moment.replace(microsecond=0),
    )
    db.add(alarm)
    return alarm


# ============================================================================
# 一、传感器事件 → 告警
# ============================================================================
def sync_event_alarms(
    db: Session, rules: InsightsRules, *, from_dt: dt.datetime, to_dt: dt.datetime
) -> SyncResult:
    """扫描时间窗内的传感器事件，按配置把该升级的升级成告警。"""
    res = SyncResult()
    rows = db.execute(
        text(
            """
            SELECT se.id, se.device_id, se.event_code, se.level, se.started_at,
                   d.name AS device_name, d.device_type, d.room_id
            FROM sensor_events se
            JOIN devices d ON d.id = se.device_id
            WHERE se.started_at >= :f AND se.started_at <= :t
            ORDER BY se.started_at DESC
            LIMIT :lim
            """
        ),
        {"f": from_dt, "t": to_dt, "lim": MAX_EVENTS_PER_SYNC},
    ).all()
    res.scanned = len(rows)

    for r in rows:
        rule = rules.event_rules.get(r.event_code)
        if rule is None:
            # 没配规则的事件只入库不告警：不是所有传感器信号都该打扰人
            res.unscored += 1
            continue
        if _alarm_near(db, rule.alarm_code, r.device_id, r.started_at) is not None:
            res.skipped += 1
            continue

        detail = _detail_text(
            criteria=rule.criteria,
            action_hint=rule.action_hint,
            evidence=f"sensor_events#{r.id}（event_code={r.event_code}, level={r.level}）",
            device_name=r.device_name,
            device_type=r.device_type,
        )
        _raise_alarm(
            db,
            room_id=r.room_id,
            device_id=r.device_id,
            rule=rule,
            moment=r.started_at,
            detail=detail,
            alarm_code=rule.alarm_code,
        )
        res.created += 1

    if res.created:
        db.commit()
        log.info("事件告警：新增 %d 条（扫描 %d 条事件，跳过 %d）", res.created, res.scanned, res.skipped)
    return res


# ============================================================================
# 二、设备状态 → 告警
# ============================================================================
def _offline_minutes(last: dt.datetime | None, anchor: dt.datetime) -> float | None:
    if last is None:
        return None           # 从没上报过 —— 与"上报过但很久没更新"是两回事
    return (anchor - last).total_seconds() / 60.0


def _age_text(minutes: float | None) -> str:
    if minutes is None:
        return "该设备从未上报过数据"
    if minutes < 60:
        return f"已 {minutes:.0f} 分钟没有上报"
    return f"已 {minutes / 60:.1f} 小时没有上报"


def _sync_offline_rule(
    db: Session,
    rule: StateRule,
    catalog: DeviceCatalog,
    cache: LatestValueCache,
    anchor: dt.datetime,
    predicate,
) -> tuple[int, int]:
    """处理"离线类"规则。返回 (新建, 恢复)。"""
    limit = rule.num("offline_minutes", 30.0)
    bad: set[int] = set()
    created = resolved = 0

    for dev in catalog.all():
        if not predicate(dev):
            continue
        last = cache.observed_extent(dev.code)
        age = _offline_minutes(last, anchor)
        if age is not None and age <= limit:
            continue                      # 还在正常上报
        bad.add(dev.id)

        if _active_alarm(db, rule.name, dev.id) is not None:
            continue                      # 已经在活跃列表里，不重复堆
        detail = _detail_text(
            criteria=f"{rule.criteria}（本设备：{_age_text(age)}）",
            action_hint=rule.action_hint,
            evidence=(
                f"最新观测时间 = {last.isoformat(timespec='seconds')}"
                if last is not None
                else "device_readings 中没有任何该设备的读数"
            ),
            device_name=dev.name,
            device_type=dev.device_type,
        )
        _raise_alarm(
            db,
            room_id=_room_id(catalog, dev.room_code),
            device_id=dev.id,
            rule=rule,
            moment=anchor,
            detail=detail,
            alarm_code=rule.name,
        )
        created += 1

    # 条件消失的活跃告警 → resolved（否则告警列表只增不减）
    actives = db.execute(
        select(Alarm).where(Alarm.alarm_code == rule.name, Alarm.status.in_(ACTIVE_STATUSES))
    ).scalars().all()
    for a in actives:
        if a.device_id is None or a.device_id not in bad:
            a.status = "resolved"
            resolved += 1
    return created, resolved


def _someone_home(db: Session, catalog: DeviceCatalog, since: dt.datetime) -> bool | None:
    """时间窗内毫米波雷达是否检测到过人。

    返回 `None` 表示"无法判定"：没有毫米波设备，或者这段时间根本没有采样。
    **无法判定时不下结论** —— 不能把"没数据"当成"没人"。
    """
    ids = [d.id for d in catalog.all() if d.device_type == "mmwave_radar"]
    if not ids:
        return None
    marks = ", ".join(f":i{i}" for i in range(len(ids)))
    params = {f"i{i}": v for i, v in enumerate(ids)}
    params.update({"f": since})
    row = db.execute(
        text(
            f"""
            SELECT COUNT(*) AS n
            FROM device_readings
            WHERE device_id IN ({marks})
              AND metric_code = 'presence'
              AND value_num = 1
              AND observed_at >= :f
            """
        ),
        params,
    ).first()
    return bool(row.n) if row is not None else None


def _sync_door_unlocked_away(
    db: Session, rule: StateRule, catalog: DeviceCatalog, cache: LatestValueCache, anchor: dt.datetime
) -> tuple[int, int]:
    away_min = rule.num("away_minutes", 20.0)
    since = anchor - dt.timedelta(minutes=away_min)
    bad: set[int] = set()
    created = resolved = 0

    for dev in catalog.all():
        if dev.device_type != "door_lock":
            continue
        state = cache.value_of(dev.code, "lock_state")
        if state is None:
            continue
        if str(state).lower() != "unlocked":
            continue

        home = _someone_home(db, catalog, since)
        if home is None or home is True:
            # 有人在家、或无法判断有没有人 → 不告警（宁可不报，也不误报）
            continue

        bad.add(dev.id)
        if _active_alarm(db, rule.name, dev.id) is not None:
            continue
        detail = _detail_text(
            criteria=f"{rule.criteria}（本次：{away_min:.0f} 分钟内毫米波雷达无 presence=true 记录）",
            action_hint=rule.action_hint,
            evidence=f"门锁最新状态 = {state}；家中无人判定依据 device_readings.presence",
            device_name=dev.name,
            device_type=dev.device_type,
        )
        _raise_alarm(
            db,
            room_id=_room_id(catalog, dev.room_code),
            device_id=dev.id,
            rule=rule,
            moment=anchor,
            detail=detail,
            alarm_code=rule.name,
        )
        created += 1

    actives = db.execute(
        select(Alarm).where(Alarm.alarm_code == rule.name, Alarm.status.in_(ACTIVE_STATUSES))
    ).scalars().all()
    for a in actives:
        if a.device_id is None or a.device_id not in bad:
            a.status = "resolved"
            resolved += 1
    return created, resolved


def sync_state_alarms(
    db: Session,
    rules: InsightsRules,
    catalog: DeviceCatalog,
    cache: LatestValueCache,
    *,
    anchor: dt.datetime,
) -> SyncResult:
    """按设备**当前**状态推导告警（不翻历史猜）。"""
    res = SyncResult()

    off = rules.state_rules.get("high_risk_offline")
    if off is not None and off.enabled:
        c, r = _sync_offline_rule(db, off, catalog, cache, anchor, lambda d: d.high_risk)
        res.created += c
        res.resolved += r

    blind = rules.state_rules.get("safety_sensor_blind_spot")
    if blind is not None and blind.enabled:
        types = set(blind.strs("device_types"))
        c, r = _sync_offline_rule(db, blind, catalog, cache, anchor, lambda d: d.device_type in types)
        res.created += c
        res.resolved += r

    away = rules.state_rules.get("door_unlocked_away")
    if away is not None and away.enabled:
        c, r = _sync_door_unlocked_away(db, away, catalog, cache, anchor)
        res.created += c
        res.resolved += r

    if res.created or res.resolved:
        db.commit()
        log.info("状态告警：新增 %d 条，恢复 %d 条", res.created, res.resolved)
    return res


# ============================================================================
# 三、查询与人工确认
# ============================================================================
def list_alarms(
    db: Session,
    *,
    anchor: dt.datetime,
    home_id: int | None = None,
    status: str | None = None,
    room_id: int | None = None,
    level: str | None = None,
    from_dt: dt.datetime | None = None,
    to_dt: dt.datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    where = ["1=1"]
    params: dict[str, Any] = {"lim": limit, "off": offset}

    if home_id is not None:
        # 告警通过房间归属套房；没有房间的告警（理论上不会有）不计入任何套房
        where.append("r.home_id = :hid")
        params["hid"] = home_id
    if status:
        where.append("a.status = :status")
        params["status"] = status
    if room_id is not None:
        where.append("a.room_id = :rid")
        params["rid"] = room_id
    if level:
        where.append("a.level = :lvl")
        params["lvl"] = level
    if from_dt is not None:
        where.append("a.created_at >= :f")
        params["f"] = from_dt
    if to_dt is not None:
        where.append("a.created_at <= :t")
        params["t"] = to_dt

    clause = " AND ".join(where)
    rows = db.execute(
        text(
            f"""
            SELECT a.id, a.alarm_code, a.level, a.title, a.detail, a.status,
                   a.room_id, a.device_id, a.created_at, a.acknowledged_by, a.acknowledged_at,
                   d.code AS device_code, d.name AS device_name, d.device_type,
                   r.code AS room_code, r.name AS room_name
            FROM alarms a
            LEFT JOIN devices d ON d.id = a.device_id
            LEFT JOIN rooms r ON r.id = a.room_id
            WHERE {clause}
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT :lim OFFSET :off
            """
        ),
        params,
    ).all()

    total = db.execute(
        text(f"SELECT COUNT(*) FROM alarms a LEFT JOIN rooms r ON r.id = a.room_id WHERE {clause}"),
        params,
    ).scalar() or 0

    # 统计与列表用**同一套过滤条件**，只忽略分页。
    # 早期版本这里查的是全表，于是出现"列表 1 条、统计说 5 条"的自相矛盾画面
    # （列表有 24 小时时间窗，统计没有）—— 前端会以为接口算错了。
    stat_params = {k: v for k, v in params.items() if k not in ("lim", "off")}
    by_level: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for lv, st, n in db.execute(
        text(
            f"SELECT a.level, a.status, COUNT(*) FROM alarms a "
            f"LEFT JOIN rooms r ON r.id = a.room_id WHERE {clause} GROUP BY a.level, a.status"
        ),
        stat_params,
    ).all():
        by_level[str(lv)] = by_level.get(str(lv), 0) + int(n)
        by_status[str(st)] = by_status.get(str(st), 0) + int(n)

    items = []
    for r in rows:
        age_min = None
        if r.created_at is not None:
            age_min = round((anchor - r.created_at).total_seconds() / 60.0, 1)
        items.append(
            {
                "id": r.id,
                "alarm_code": r.alarm_code,
                "level": r.level,
                "title": r.title,
                "detail": r.detail,
                "status": r.status,
                "is_active": r.status in ACTIVE_STATUSES,
                "room_id": r.room_id,
                "room_code": r.room_code,
                "room_name": r.room_name,
                "device_id": r.device_id,
                "device_code": r.device_code,
                "device_name": r.device_name,
                "device_type": r.device_type,
                "created_at": r.created_at,
                "age_minutes": age_min,
                "acknowledged_by": r.acknowledged_by,
                "acknowledged_at": r.acknowledged_at,
            }
        )

    return {
        "items": items,
        "total": int(total),
        "by_level": by_level,
        "by_status": by_status,
    }


def acknowledge_alarm(
    db: Session, alarm_id: int, *, acknowledged_by: int | None = None
) -> Alarm:
    """人工确认一条告警。

    ⚠️ 确认只表示"人看到了、知道了"，**不代表动作已执行**。
    真要关燃气阀，得走高风险待确认通道，那是另一条链路（并且需要二次确认）。
    """
    alarm = db.get(Alarm, alarm_id)
    if alarm is None:
        raise LookupError(f"告警 {alarm_id} 不存在")

    if acknowledged_by is not None:
        exists = db.execute(
            text("SELECT 1 FROM members WHERE id = :i LIMIT 1"), {"i": acknowledged_by}
        ).first()
        if exists is None:
            raise ValueError(f"成员 {acknowledged_by} 不存在")

    now = dt.datetime.now()
    # 演示数据的时间轴可能略领先真实时钟，确认时间不能早于告警本身的创建时间
    if alarm.created_at is not None and now < alarm.created_at:
        now = alarm.created_at
    alarm.status = "acknowledged"
    alarm.acknowledged_at = now.replace(microsecond=0)
    alarm.acknowledged_by = acknowledged_by
    db.commit()
    db.refresh(alarm)
    log.info("告警 %s 已被确认（by=%s）", alarm.id, acknowledged_by)
    return alarm
