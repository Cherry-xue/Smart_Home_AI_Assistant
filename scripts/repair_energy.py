"""修复"补历史顺序错误"导致的能耗增量为空。

问题现象
--------
`energy_readings.energy_kwh` 大量为 NULL，于是能耗页显示 0.00 kWh，
而 `power_w` 明明是对的（热水器 1800W、空调 1100W）。

根因（不是采集逻辑的错，是操作顺序的错）
--------------------------------------
能耗是**积分量**：`energy_kwh = power_w × 与上一条的间隔 / 3600 / 1000`。
采集端对"时间倒退"的处理是**不计**（`gap <= 0` 直接跳过），否则会算出负电量。

补历史必须**在后端启动之初**跑（`start_dev.py --backfill` 就是这么排的）。
如果先让常驻生成器把时间线推到"现在"，再回头补 24 小时历史，
整批数据在采集端看来全是"时间倒退"→ 全部不计增量 → 电量恒为 0。

⚠️ 这个脚本修的是**派生列**，不碰原始功率读数：
   它按 `observed_at` 相邻两条的间隔，用已存的 `power_w` 重新积分。
   间隔不合理（<=0 或 >60s）的行仍然留空 —— 那段功率未知，不凭空补。

用法：
    python scripts/repair_energy.py                 # 只看会修多少（dry-run）
    python scripts/repair_energy.py --apply         # 真的写库
    python scripts/repair_energy.py --apply --since 2026-10-16T19:30:00
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402

#: 合理的采样间隔上限（秒）。超过说明中间断连，那段功率未知。
MAX_GAP_SEC = 60
#: 功率上限（W）—— 家用单设备不可能更高；防止脏数据把电量放大
MAX_POWER_W = 5000.0


def main() -> int:
    ap = argparse.ArgumentParser(description="重算能耗增量（修复补历史顺序导致的全零）")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只预览）")
    ap.add_argument("--since", default=None, help="只修这个时刻之后的数据（缺省=最近 48 小时）")
    ap.add_argument("--hours", type=float, default=48.0, help="从最新数据往前多少小时")
    args = ap.parse_args()

    with SessionLocal() as db:
        latest = db.execute(text("SELECT MAX(observed_at) FROM energy_readings")).scalar()
        if latest is None:
            print("❌ energy_readings 是空的，没什么可修")
            return 1
        since = args.since or str(latest.replace(microsecond=0))
        if args.since is None:
            since_sql = f"DATE_SUB('{latest}', INTERVAL {int(args.hours)} HOUR)"
        else:
            since_sql = f"'{args.since}'"

        print("=" * 72)
        print("能耗增量修复")
        print("=" * 72)
        print(f"  最新数据时间 : {latest}")
        print(f"  修复区间     : {since_sql} → 最新")
        print()

        before = db.execute(
            text(f"SELECT COUNT(*), SUM(energy_kwh) FROM energy_readings WHERE observed_at >= {since_sql}")
        ).first()
        nulls = db.execute(
            text(
                f"SELECT COUNT(*) FROM energy_readings "
                f"WHERE observed_at >= {since_sql} AND energy_kwh IS NULL"
            )
        ).scalar()
        print(f"  区间内行数   : {before[0]:,}")
        print(f"  其中增量为空 : {nulls:,}")
        print(f"  当前总电量   : {float(before[1] or 0):.4f} kWh")
        print()

        # 用窗口函数取"同一设备的上一条"，间隔合理才补
        repair_sql = f"""
            UPDATE energy_readings e
            JOIN (
                SELECT id,
                       LAG(observed_at) OVER (PARTITION BY device_id ORDER BY observed_at, id) AS prev_at,
                       TIMESTAMPDIFF(
                           SECOND,
                           LAG(observed_at) OVER (PARTITION BY device_id ORDER BY observed_at, id),
                           observed_at
                       ) AS gap_sec
                FROM energy_readings
                WHERE observed_at >= {since_sql}
            ) t ON t.id = e.id
            SET e.energy_kwh = LEAST(e.power_w, {MAX_POWER_W}) * t.gap_sec / 3600000
            WHERE e.energy_kwh IS NULL
              AND t.gap_sec IS NOT NULL
              AND t.gap_sec > 0
              AND t.gap_sec <= {MAX_GAP_SEC}
              AND e.power_w IS NOT NULL
        """

        if not args.apply:
            # dry-run：先算一遍"如果修，会补多少行"，不改库
            preview = db.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM energy_readings e
                    JOIN (
                        SELECT id,
                               TIMESTAMPDIFF(
                                   SECOND,
                                   LAG(observed_at) OVER (PARTITION BY device_id ORDER BY observed_at, id),
                                   observed_at
                               ) AS gap_sec
                        FROM energy_readings
                        WHERE observed_at >= {since_sql}
                    ) t ON t.id = e.id
                    WHERE e.energy_kwh IS NULL
                      AND t.gap_sec IS NOT NULL AND t.gap_sec > 0 AND t.gap_sec <= {MAX_GAP_SEC}
                      AND e.power_w IS NOT NULL
                    """
                )
            ).scalar()
            print(f"  [dry-run] 可补 {preview:,} 行；加 --apply 才会写库")
            db.rollback()
            return 0

        res = db.execute(text(repair_sql))
        db.commit()
        print(f"  ✓ 已补 {res.rowcount:,} 行的增量电量")

        after = db.execute(
            text(f"SELECT COUNT(*), SUM(energy_kwh) FROM energy_readings WHERE observed_at >= {since_sql}")
        ).first()
        print(f"  修复后总电量 : {float(after[1] or 0):.4f} kWh")
        print()
        print("  ⚠️ 说明：这是按已存的 power_w 与观测间隔重新积分，")
        print("     不是编数据；间隔不合理的行仍然留空。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
