"""建第二套演示房（工作室单间），用来验证"多套房切换"。

为什么需要它
------------
多套房切换这个功能，只有一套房时**看不出任何效果**（切了还是同一套）。
所以这里建一套结构不同的小房子：2 个房间 + 6 台设备，
而且房间编码是新的（`studio_work` / `studio_rest`），
正好能验证"每套房有自己的户型图和房间"。

幂等：已经存在（按名称判定）就直接跳过，不会重复建。

用法：
    python scripts/seed_second_home.py          # 建（已存在则跳过）
    python scripts/seed_second_home.py --reset  # 先删掉这套房再建
                                               # ⚠️ 会连带删除它的设备与历史数据
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.services.device_admin import add_device  # noqa: E402

HOME_NAME = "工作室单间 · 一室一厅"

ROOMS = [
    # (code, name, floor, sort_no)
    ("studio_work", "工作区", 1, 1),
    ("studio_rest", "休息区", 1, 2),
]

DEVICES = [
    # (preset_key, room_code, name)
    ("light", "studio_work", "工作区主灯"),
    ("temp_sensor", "studio_work", "工作区温湿度"),
    ("curtain", "studio_work", "工作区窗帘"),
    ("light", "studio_rest", "休息区主灯"),
    ("ac", "studio_rest", "休息区空调"),
    ("temp_sensor", "studio_rest", "休息区温湿度"),
]


def find_home(db) -> int | None:
    row = db.execute(text("SELECT id FROM homes WHERE name = :n"), {"n": HOME_NAME}).first()
    return int(row[0]) if row else None


def purge(db, home_id: int) -> None:
    """删掉整套房的数据。⚠️ 顺序很重要：子表先删，否则外键会挡住。"""
    dev_ids = [r[0] for r in db.execute(
        text("SELECT id FROM devices WHERE home_id = :h"), {"h": home_id}
    ).all()]
    for did in dev_ids:
        for t in ("device_readings", "sensor_events", "device_commands", "energy_readings", "alarms"):
            db.execute(text(f"DELETE FROM {t} WHERE device_id = :d"), {"d": did})
        db.execute(text("DELETE FROM device_capabilities WHERE device_id = :d"), {"d": did})
    db.execute(text("DELETE FROM devices WHERE home_id = :h"), {"h": home_id})
    db.execute(text("DELETE FROM rooms WHERE home_id = :h"), {"h": home_id})
    db.execute(text("DELETE FROM homes WHERE id = :h"), {"h": home_id})
    db.commit()
    print(f"  已删除套房 {home_id}（设备 {len(dev_ids)} 台及其历史数据）")


def main() -> int:
    ap = argparse.ArgumentParser(description="建第二套演示房")
    ap.add_argument("--reset", action="store_true", help="先删除再重建（会删历史数据）")
    args = ap.parse_args()

    with SessionLocal() as db:
        existing = find_home(db)
        if existing is not None and not args.reset:
            print(f"✓ 套房已存在（id={existing}），跳过。用 --reset 可重建。")
            return 0
        if existing is not None:
            purge(db, existing)

        db.execute(
            text(
                "INSERT INTO homes (name, timezone, address_hash) "
                "VALUES (:n, 'Asia/Shanghai', NULL)"
            ),
            {"n": HOME_NAME},
        )
        db.commit()
        home_id = find_home(db)
        assert home_id is not None
        print(f"  已建套房 id={home_id}「{HOME_NAME}」")

        room_ids: dict[str, int] = {}
        for code, name, floor, sort_no in ROOMS:
            db.execute(
                text(
                    "INSERT INTO rooms (home_id, code, name, floor, sort_no) "
                    "VALUES (:h, :c, :n, :f, :s)"
                ),
                {"h": home_id, "c": code, "n": name, "f": floor, "s": sort_no},
            )
            db.commit()
            row = db.execute(
                text("SELECT id FROM rooms WHERE home_id = :h AND code = :c"),
                {"h": home_id, "c": code},
            ).first()
            room_ids[code] = int(row[0])
            print(f"    · 房间 {code}「{name}」id={room_ids[code]}")

        for preset_key, room_code, name in DEVICES:
            dev, preset = add_device(
                db, preset_key=preset_key, home_id=home_id,
                room_id=room_ids[room_code], name=name,
            )
            cap_n = db.execute(
                text("SELECT COUNT(*) FROM device_capabilities WHERE device_id = :d"),
                {"d": dev.id},
            ).scalar()
            print(f"    · 设备 {dev.code:<18} {dev.name:<14} 能力 {cap_n} 项"
                  f"{'  ⚠️ 高风险' if dev.high_risk else ''}")

        print()
        print("  ✓ 完成。重启后端 + 生成器后，这套房就会有数据：")
        print("      · 生成器按每台设备自己的 home_id 发布（已支持）")
        print("      · 总览页顶部可以切换套房，户型图按套房独立保存")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
