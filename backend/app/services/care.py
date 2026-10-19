"""照护观察：从**非摄像头**感知数据推导活动节律。

数据来源与边界
--------------
只用两类信号：毫米波雷达的 `presence` / `activity_level`，以及门磁的开合。
**不使用任何摄像头**，也不做人脸/身份识别 —— 这是需求分析里就定下的边界。

为什么不能只看 presence（这是实测踩出来的）
------------------------------------------
第一版按 `presence=1` 判"活动"，结果把凌晨 23:55–06:55 **连续 425 分钟**记成了
"夜间活动"。去查数据才发现：人躺在床上睡觉时 presence 也是 1 —— 那是"房间里有人"，
不是"有人在活动"。

生成器的 `activity_level` 本身分了三档（0~12 睡觉 / 25~55 醒着静止 / 60~100 走动），
所以这里用 `active_activity_level`（默认 20，正好落在空隙里）把三种状态分开：

    无人在场 → absent      没有 presence
    在床静息 → in_bed      有 presence，但活动量低于阈值
    醒着活动 → awake       有 presence，且活动量达标

只有 `awake` 才算"活动"，`in_bed` 单独记一条"在床静息"。
"长时间无活动"只统计 **absent 且发生在白天** 的时段 —— 夜里安静是正常的。

措辞红线
--------
所有输出的定位是「**待确认线索**」，不是健康结论、更不是医疗判断：
- 说"连续 4 小时无活动迹象"，不说"可能昏迷/跌倒"；
- 说"待联系核实"，不说"建议就医"；
- `confidence` 表达的是**数据可信度**（采样是否完整），不是"生病的概率"。

⚠️ 另一个容易想当然的坑
----------------------
"没有 presence 采样" 有两种完全不同的原因：
① 真的没人活动；② **传感器根本没在发数据**。
把 ② 当成 ① 就是纯粹的误导。所以这里先算**覆盖率**（实际采样桶 / 应有桶），
覆盖率不足时**不做节律判断**，只如实报告"这段数据不够，判断不了"。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.core.insights_rules import CareSubject, CareThresholds, InsightsRules
from app.db.catalog import DeviceCatalog
from app.models.orm import CareObservation

log = logging.getLogger(__name__)

#: （已移除）原先响应里带一句「不构成健康或医疗判断，需人工核实」的免责话：
#: 界面上不显示，且"不做医疗断言"本来就是措辞红线（见 tests/m6_check 的 banned_assert），
#: 靠文案约束即可，不必再单独挂一句免责。

#: 活动类型 → 人可读标签
ACTIVITY_LABELS = {
    "wake_up": "起床",
    "night_activity": "夜间活动",
    "activity": "日常活动",
    "in_bed": "在床静息",
    "no_activity": "长时间无活动",
    "insufficient_data": "数据不足，无法判断",
}

#: 需要人关注的类型（其余是正常节律记录）
CLUE_TYPES = ("no_activity", "insufficient_data")
#: 不算"活动"的类型（用于算 last_activity_at）
NON_ACTIVITY_TYPES = ("in_bed", "no_activity", "insufficient_data")

#: 桶的三种状态
ST_ABSENT = "absent"
ST_IN_BED = "in_bed"
ST_AWAKE = "awake"

#: 覆盖率低于这个值就判定"数据不足以推断节律"
MIN_COVERAGE_RATIO = 0.5


@dataclasses.dataclass(frozen=True)
class SubjectCoverage:
    """一个照护对象的分析覆盖情况，如实告诉调用方"这段结论有多少数据支撑"。"""

    member_id: int
    nickname: str
    device_code: str
    window_from: dt.datetime
    window_to: dt.datetime
    buckets_expected: int
    buckets_actual: int
    buckets_with_presence: int
    ratio: float
    sufficient: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "nickname": self.nickname,
            "device_code": self.device_code,
            "window": {"from": self.window_from, "to": self.window_to},
            "buckets_expected": self.buckets_expected,
            "buckets_actual": self.buckets_actual,
            "buckets_with_presence": self.buckets_with_presence,
            "coverage_ratio": self.ratio,
            "sufficient": self.sufficient,
        }


@dataclasses.dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    subjects: list[SubjectCoverage] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "subjects": [s.as_dict() for s in self.subjects],
        }


# ============================================================================
# 内部：读数据 / 分段 / 分类
# ============================================================================
def _presence_buckets(
    db: Session, device_id: int, from_dt: dt.datetime, to_dt: dt.datetime, bucket_min: int
) -> list[tuple[dt.datetime, bool, float | None]]:
    """按固定粒度聚合，返回 [(桶起始时刻, 是否有人, 活动量均值)]。

    ⚠️ 为什么在 SQL 里聚合：生成器每 5 秒一条读数，24 小时就是一万七千行。
    拉进 Python 再按 5 分钟压一遍纯属浪费，而活动节律本来就是分钟级分辨率。

    `FROM_UNIXTIME(...)` 让桶时间在 MySQL 自己的时区口径里算完再返回，
    避免"Python 用本地时区再解释一次"导致整体偏移。

    presence 与 activity_level 是**两行不同的 metric**，所以用条件聚合一次取出，
    保证两者在同一个桶里对齐。
    """
    rows = db.execute(
        text(
            """
            SELECT FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(observed_at) / :b) * :b) AS bucket,
                   MAX(CASE WHEN metric_code = 'presence' THEN value_num END) AS present,
                   AVG(CASE WHEN metric_code = 'activity_level' THEN value_num END) AS activity,
                   COUNT(*) AS n
            FROM device_readings
            WHERE device_id = :did
              AND metric_code IN ('presence', 'activity_level')
              AND observed_at >= :f
              AND observed_at <= :t
            GROUP BY bucket
            ORDER BY bucket
            """
        ),
        {"did": device_id, "f": from_dt, "t": to_dt, "b": bucket_min * 60},
    ).all()

    out: list[tuple[dt.datetime, bool, float | None]] = []
    for r in rows:
        raw = r.bucket
        moment = raw if isinstance(raw, dt.datetime) else dt.datetime.fromisoformat(str(raw))
        present = bool(r.present)
        activity = float(r.activity) if r.activity is not None else None
        out.append((moment, present, activity))
    return out


def _bucket_state(present: bool, activity: float | None, th: CareThresholds) -> str:
    """把桶归类成 无人在场 / 在床静息 / 醒着活动。"""
    if not present:
        return ST_ABSENT
    if activity is None:
        # 只有 presence、没有 activity_level（设备能力缺失或那条消息丢了）：
        # 保守当作"有人"，但不据此推断"在活动"。宁可少报，也不误报。
        return ST_IN_BED
    return ST_AWAKE if activity >= th.active_activity_level else ST_IN_BED


def _runs(
    buckets: list[tuple[dt.datetime, bool, float | None]],
    gap_min: float,
    *,
    want: str,
    th: CareThresholds,
) -> list[tuple[dt.datetime, dt.datetime, int]]:
    """取出所有"状态为 want"的连续段，返回 [(起, 止, 桶数)]。

    小于 `gap_min` 的空档会被跨过去（同一个人的连续活动中间会有短暂静止）。
    """
    out: list[tuple[dt.datetime, dt.datetime, int]] = []
    start: dt.datetime | None = None
    last: dt.datetime | None = None
    count = 0
    gap = dt.timedelta(minutes=gap_min)

    for moment, present, activity in buckets:
        if _bucket_state(present, activity, th) != want:
            continue
        if start is None:
            start, count = moment, 1
        elif last is not None and moment - last > gap:
            out.append((start, last, count))
            start, count = moment, 1
        else:
            count += 1
        last = moment
    if start is not None and last is not None:
        out.append((start, last, count))
    return out


def _span_minutes(start: dt.datetime, end: dt.datetime, bucket_min: float) -> int:
    """段的时长（把最后一个桶本身也算进去）。"""
    return int((end - start).total_seconds() // 60) + int(bucket_min)


def _classify_awake(moment: dt.datetime, th: CareThresholds) -> str:
    """醒着活动的这一段算什么类型。"""
    t = moment.time()
    if th.is_night(t):
        return "night_activity"     # 夜里醒着动 = 起夜/夜间活动
    if th.is_morning(t):
        return "wake_up"            # 早上第一段活动 = 起床
    return "activity"


def _confidence(bucket_count: int, duration_min: float, sufficient: bool, th: CareThresholds) -> float:
    """可信度 = 采样密度 × 覆盖充分性。

    刻意叫 confidence 而不是"风险"：它衡量的是**这段观察有多可信**，
    与"老人健康状况好不好"没有任何关系。把两者混在一起最容易造成误导。
    """
    if not sufficient:
        return 0.300
    expected = max(duration_min / max(th.bucket_minutes, 1.0), 1.0)
    density = min(bucket_count / expected, 1.0)
    return round(min(0.55 + 0.4 * density, 0.950), 3)


def _upsert(
    db: Session,
    *,
    member_id: int,
    observed_at: dt.datetime,
    activity_type: str,
    duration_min: int | None,
    source_device: int | None,
    confidence: float,
    remark: str,
) -> bool:
    """按 (成员, 类型, 观察时刻) 幂等写入。返回 True=新建，False=更新。"""
    existing = db.execute(
        select(CareObservation)
        .where(
            CareObservation.member_id == member_id,
            CareObservation.activity_type == activity_type,
            CareObservation.observed_at == observed_at,
        )
        .limit(1)
    ).scalars().first()

    if existing is None:
        db.add(
            CareObservation(
                member_id=member_id,
                observed_at=observed_at,
                activity_type=activity_type,
                duration_min=duration_min,
                source_device=source_device,
                confidence=confidence,
                remark=remark[:255],
            )
        )
        return True

    existing.duration_min = duration_min
    existing.confidence = confidence
    existing.remark = remark[:255]
    existing.source_device = source_device
    return False


def _member_id(db: Session, nickname: str) -> int | None:
    row = db.execute(
        text("SELECT id FROM members WHERE nickname = :n AND enabled = 1 ORDER BY id LIMIT 1"),
        {"n": nickname},
    ).first()
    return int(row[0]) if row else None


# ============================================================================
# 一、推导
# ============================================================================
def _sync_subject(
    db: Session,
    subject: CareSubject,
    th: CareThresholds,
    catalog: DeviceCatalog,
    anchor: dt.datetime,
) -> tuple[int, int, SubjectCoverage | None]:
    member_id = _member_id(db, subject.member_nickname)
    if member_id is None:
        log.warning("照护对象「%s」在 members 表里找不到，跳过", subject.member_nickname)
        return 0, 0, None

    dev = catalog.get(subject.presence_device)
    if dev is None:
        log.warning("照护感知设备 %s 不在设备目录里，跳过", subject.presence_device)
        return 0, 0, None

    from_dt = anchor - dt.timedelta(hours=th.window_hours)
    buckets = _presence_buckets(db, dev.id, from_dt, anchor, int(th.bucket_minutes))

    expected = max(int(th.window_hours * 60 / th.bucket_minutes), 1)
    actual = len(buckets)
    with_presence = sum(1 for _, p, _ in buckets if p)
    ratio = round(actual / expected, 3)
    sufficient = ratio >= MIN_COVERAGE_RATIO and actual > 0

    coverage = SubjectCoverage(
        member_id=member_id,
        nickname=subject.member_nickname,
        device_code=dev.code,
        window_from=from_dt,
        window_to=anchor,
        buckets_expected=expected,
        buckets_actual=actual,
        buckets_with_presence=with_presence,
        ratio=ratio,
        sufficient=sufficient,
    )

    created = updated = 0

    # ⚠️ `insufficient_data` 是**当前状态标记**，不是历史观察：
    # 它只在"这段数据不够、判断不了"时为真。覆盖率恢复之后还留着它，
    # 时间轴上就会出现一行莫名其妙的"数据不足"，而同期明明有正常记录。
    # 所以每次推导先清掉旧标记，需要时再写一条新的（保证同成员最多一条）。
    db.execute(
        delete(CareObservation).where(
            CareObservation.member_id == member_id,
            CareObservation.activity_type == "insufficient_data",
        )
    )

    if not sufficient:
        # 数据不够就不猜。只写一条"无法判断"的说明，让界面能如实显示。
        remark = f"感知覆盖不足（{actual}/{expected} 桶，{ratio:.0%}），未做节律判断"
        is_new = _upsert(
            db,
            member_id=member_id,
            observed_at=anchor.replace(microsecond=0),
            activity_type="insufficient_data",
            duration_min=None,
            source_device=dev.id,
            confidence=0.0,
            remark=remark,
        )
        created += int(is_new)
        updated += int(not is_new)
        db.commit()
        return created, updated, coverage

    # ---- 醒着活动 ----
    for start, end, count in _runs(buckets, th.segment_gap_minutes, want=ST_AWAKE, th=th):
        if count < th.min_samples_per_segment:
            continue
        duration = _span_minutes(start, end, th.bucket_minutes)
        kind = _classify_awake(start, th)
        label = ACTIVITY_LABELS.get(kind, kind)
        remark = (
            f"{start:%H:%M}–{end:%H:%M} 活动 {count} 桶（活动量 >{th.active_activity_level:.0f}），"
            f"归类为{label}"
        )
        is_new = _upsert(
            db,
            member_id=member_id,
            observed_at=start.replace(microsecond=0),
            activity_type=kind,
            duration_min=duration,
            source_device=dev.id,
            confidence=_confidence(count, duration, sufficient, th),
            remark=remark,
        )
        created += int(is_new)
        updated += int(not is_new)

    # ---- 在床静息（presence=1 但活动量低）----
    for start, end, count in _runs(buckets, th.segment_gap_minutes, want=ST_IN_BED, th=th):
        duration = _span_minutes(start, end, th.bucket_minutes)
        if duration < th.bed_minutes:
            continue
        remark = (
            f"{start:%H:%M}–{end:%H:%M} 活动量低于 {th.active_activity_level:.0f}"
            f"（持续 {duration} 分钟），记为在床/静息"
        )
        is_new = _upsert(
            db,
            member_id=member_id,
            observed_at=start.replace(microsecond=0),
            activity_type="in_bed",
            duration_min=duration,
            source_device=dev.id,
            confidence=_confidence(count, duration, sufficient, th),
            remark=remark,
        )
        created += int(is_new)
        updated += int(not is_new)

    # ---- 长时间无活动：只看白天，夜里的安静是正常的 ----
    need_min = int(th.no_activity_hours * 60)
    for start, end, count in _runs(buckets, th.segment_gap_minutes, want=ST_ABSENT, th=th):
        duration = _span_minutes(start, end, th.bucket_minutes)
        if duration < need_min:
            continue
        if th.is_night(start.time()):
            # 夜间没人（可能只是没在房间里/在别的房间），不作为线索
            continue
        hours, mins = divmod(duration, 60)
        span_text = f"{hours} 小时" + (f" {mins} 分钟" if mins else "")
        remark = f"{start:%H:%M}–{end:%H:%M} 连续 {span_text}无人在场（{count} 桶）"
        is_new = _upsert(
            db,
            member_id=member_id,
            observed_at=start.replace(microsecond=0),
            activity_type="no_activity",
            duration_min=duration,
            source_device=dev.id,
            confidence=0.800,
            remark=remark,
        )
        created += int(is_new)
        updated += int(not is_new)

    if created or updated:
        db.commit()
    return created, updated, coverage


def sync_observations(
    db: Session, rules: InsightsRules, catalog: DeviceCatalog, *, anchor: dt.datetime
) -> SyncResult:
    res = SyncResult()
    for subject in rules.subjects:
        c, u, cov = _sync_subject(db, subject, rules.care, catalog, anchor)
        res.created += c
        res.updated += u
        if cov is not None:
            res.subjects.append(cov)
    if res.created or res.updated:
        log.info("照护观察：新增 %d 条，更新 %d 条", res.created, res.updated)
    return res


# ============================================================================
# 二、查询
# ============================================================================
def list_observations(
    db: Session,
    *,
    home_id: int | None = None,
    member_id: int | None = None,
    from_dt: dt.datetime | None = None,
    to_dt: dt.datetime | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    where = ["1=1"]
    params: dict[str, Any] = {"lim": limit}
    if home_id is not None:
        # 成员归属套房；照护观察跟着成员走
        where.append("m.home_id = :hid")
        params["hid"] = home_id
    if member_id is not None:
        where.append("o.member_id = :mid")
        params["mid"] = member_id
    if from_dt is not None:
        # ⚠️ 用"与窗口有重叠"而不是"起点在窗口内"：
        # 一段"在床静息"可能 22:20 就开始了，而查询窗口从 22:24 起 ——
        # 按起点过滤会把它整条丢掉，用户看到的是"这一夜什么都没发生"。
        where.append(
            "DATE_ADD(o.observed_at, INTERVAL COALESCE(o.duration_min, 0) MINUTE) >= :f"
        )
        params["f"] = from_dt
    if to_dt is not None:
        where.append("o.observed_at <= :t")
        params["t"] = to_dt
    clause = " AND ".join(where)

    rows = db.execute(
        text(
            f"""
            SELECT o.id, o.member_id, o.observed_at, o.activity_type, o.duration_min,
                   o.source_device, o.confidence, o.remark,
                   m.nickname, m.role_code, m.birth_year,
                   d.code AS device_code, d.name AS device_name
            FROM care_observations o
            JOIN members m ON m.id = o.member_id
            LEFT JOIN devices d ON d.id = o.source_device
            WHERE {clause}
            ORDER BY o.observed_at DESC, o.id DESC
            LIMIT :lim
            """
        ),
        params,
    ).all()

    items = [
        {
            "id": r.id,
            "member_id": r.member_id,
            "nickname": r.nickname,
            "role_code": r.role_code,
            "birth_year": r.birth_year,
            "observed_at": r.observed_at,
            "activity_type": r.activity_type,
            "activity_label": ACTIVITY_LABELS.get(r.activity_type, r.activity_type),
            "duration_min": r.duration_min,
            "source_device_id": r.source_device,
            "source_device_code": r.device_code,
            "source_device_name": r.device_name,
            "confidence": float(r.confidence) if r.confidence is not None else None,
            "remark": r.remark,
            "is_clue": r.activity_type in CLUE_TYPES,
        }
        for r in rows
    ]

    # 按成员汇总，前端画"照护卡片"直接用
    summary: dict[int, dict[str, Any]] = {}
    for it in items:
        bucket = summary.setdefault(
            it["member_id"],
            {
                "member_id": it["member_id"],
                "nickname": it["nickname"],
                "role_code": it["role_code"],
                "observations": 0,
                "clues": 0,
                "last_activity_at": None,
                "has_inactivity_clue": False,
            },
        )
        bucket["observations"] += 1
        if it["is_clue"]:
            bucket["clues"] += 1
        if it["activity_type"] == "no_activity":
            bucket["has_inactivity_clue"] = True
        if it["activity_type"] not in NON_ACTIVITY_TYPES:
            if bucket["last_activity_at"] is None or it["observed_at"] > bucket["last_activity_at"]:
                bucket["last_activity_at"] = it["observed_at"]

    return {
        "items": items,
        "total": len(items),
        "members": list(summary.values()),
    }
