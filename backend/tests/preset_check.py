"""预设设备清单 · 契约对账。

`config/device_presets.yaml` 和数据库 `device_capabilities` 是**两份描述同一件事的数据**：
前者是"新增设备时的模板"，后者是"已登记设备的能力档案"。
一旦漂移，会出现"按预设加的灯有亮度，原有的灯没有"这种诡异现象。

所以这个脚本做四件事：
1. 预设文件的每条能力，必须能在库里同类型设备的能力档案里找到**同名同型同范围**的一条；
2. 库里已有的每种设备类型，预设里**都要有**（否则新加的比不上原有的丰富）；
3. ⚠️ `high_risk` 必须由 `device_type` 推导，且与库里现有同类设备的取值一致
   （防止有人在预设里写 `high_risk: false` 把红线绕过去）；
4. 预设文件里**不允许出现 `high_risk` 字段**（`load_presets()` 会直接抛错）。

用法：
    python tests/preset_check.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.services.device_admin import (  # noqa: E402
    PRESETS_PATH,
    DeviceAdminError,
    derive_high_risk,
    load_presets,
)


class Checker:
    def __init__(self, title: str = "") -> None:
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  ✅ {name}" + (f"  [{detail}]" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  ❌ {name}" + (f"  [{detail}]" if detail else ""))


def _db_facts():
    """从库里取：每类型的能力档案 + 每类型的 high_risk 取值。"""
    s = get_settings()
    conn = pymysql.connect(
        host=s.mysql_host,
        port=s.mysql_port,
        user=s.mysql_user,
        password=s.mysql_password,
        database=s.mysql_db,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT d.device_type, dc.capability_code, dc.display_name, dc.value_type,
                   dc.unit, dc.value_min, dc.value_max, dc.enum_values,
                   dc.writable, dc.risk_level
            FROM device_capabilities dc
            JOIN devices d ON d.id = dc.device_id
            GROUP BY d.device_type, dc.capability_code, dc.display_name, dc.value_type,
                     dc.unit, dc.value_min, dc.value_max, dc.enum_values,
                     dc.writable, dc.risk_level
            """
        )
        caps: dict[str, dict[str, dict]] = {}
        for r in cur.fetchall():
            caps.setdefault(r["device_type"], {})[r["capability_code"]] = r

        cur.execute("SELECT device_type, MAX(high_risk) AS hr, COUNT(*) AS n FROM devices GROUP BY device_type")
        types = {r["device_type"]: {"high_risk": bool(r["hr"]), "count": r["n"]} for r in cur.fetchall()}
        return caps, types
    finally:
        conn.close()


def _num(v):
    return None if v is None else float(v)


def main() -> int:
    ck = Checker("预设设备清单 · 契约对账")
    print("=" * 78)
    print("预设设备清单 · 契约对账（config/device_presets.yaml ↔ device_capabilities）")
    print("=" * 78)

    print(f"\n[0] 预设文件：{PRESETS_PATH}")
    ck.check("预设文件存在", PRESETS_PATH.exists())
    if not PRESETS_PATH.exists():
        return 1

    raw_text = PRESETS_PATH.read_text(encoding="utf-8")
    # 注释里提到 high_risk 是允许的（那是在解释为什么不要它），所以按 YAML 解析后再查
    try:
        presets = load_presets(force=True)
    except DeviceAdminError as e:
        ck.check("预设文件能被解析", False, e.message)
        return 1
    ck.check("预设文件能被解析", True, f"{len(presets)} 条预设")

    import yaml  # noqa: E402  （放在这里是因为要先用 load_presets 做完整校验）

    raw = yaml.safe_load(raw_text) or {}
    leaked = [e.get("key") for e in (raw.get("presets") or []) if "high_risk" in e]
    ck.check(
        "🚨 预设里没有任何 high_risk 字段（红线：必须由类型推导）",
        not leaked,
        f"违规：{leaked}" if leaked else "无",
    )

    caps_by_type, types_in_db = _db_facts()

    # ---------------------------------------------------------------- 1. 逐条比对能力
    print("\n[1] 每条预设的能力必须与库里同类型设备**同名、同型、同范围**")
    for p in presets:
        db_caps = caps_by_type.get(p.device_type)
        if db_caps is None:
            ck.check(f"{p.key}: 库里已有该类型可比对", True, "库里暂无该类型设备，跳过（新类型）")
            continue
        for c in p.capabilities:
            code = c["code"]
            ref = db_caps.get(code)
            if ref is None:
                ck.check(f"{p.key}.{code} 存在于库里", False, "库里没有这个能力")
                continue
            same = (
                c["value_type"] == ref["value_type"]
                and _num(c["value_min"]) == _num(ref["value_min"])
                and _num(c["value_max"]) == _num(ref["value_max"])
                and str(c["enum_values"] or "") == str(ref["enum_values"] or "")
                and bool(c["writable"]) == bool(ref["writable"])
                and str(c["risk_level"]) == str(ref["risk_level"])
            )
            ck.check(
                f"{p.key}.{code} 与库一致",
                same,
                f"{c['value_type']}/{c['value_min']}~{c['value_max']}/{c['enum_values']}/w={c['writable']}/r={c['risk_level']}",
            )

    # ---------------------------------------------------------------- 2. 类型覆盖
    print("\n[2] 库里已有的每种设备类型，预设里都要有")
    preset_types = {p.device_type for p in presets}
    missing = sorted(set(types_in_db) - preset_types)
    ck.check(
        "类型覆盖完整",
        not missing,
        f"库里 {len(types_in_db)} 种 / 预设 {len(preset_types)} 种"
        + (f"；缺：{missing}" if missing else ""),
    )

    # ---------------------------------------------------------------- 3. high_risk 一致
    print("\n[3] 🚨 high_risk 推导结果必须与库里现有同类设备一致（红线）")
    by_type = {p.device_type: p for p in presets}
    for t, info in sorted(types_in_db.items()):
        p = by_type.get(t)
        if p is None:
            continue
        ck.check(
            f"{t}: 推导值 = 库里实际值（{info['high_risk']}）",
            derive_high_risk(t) == info["high_risk"],
            f"共 {info['count']} 台",
        )

    by_key = {p.key: p for p in presets}
    for t in ("door_lock", "gas_valve", "main_breaker"):
        p = next((x for x in presets if x.device_type == t), None)
        if p is not None:
            ck.check(f"{t} 在预设里标记为高风险", p.high_risk, p.key)

    # 只读传感器不该被当成高风险设备（能力 risk_level=high 说的是"数据重要"，不是"控制危险"）
    for t in ("smoke_sensor", "gas_sensor", "water_leak_sensor"):
        ck.check(f"{t} **不是**高风险设备（只读）", not derive_high_risk(t))

    # ---------------------------------------------------------------- 汇总
    print()
    print("-" * 78)
    if ck.failed:
        print(f"{ck.title}: {ck.passed} PASS / {len(ck.failed)} FAIL")
        for f in ck.failed:
            print(f"    ❌ {f}")
    else:
        print(f"{ck.title}: {ck.passed} PASS / 0 FAIL")
    print("-" * 78)
    print()
    print("=" * 78)
    if ck.failed:
        print(f"结果：{ck.passed} PASS / {len(ck.failed)} FAIL")
    else:
        print(f"结果：{ck.passed} PASS / 0 FAIL  —— 预设清单与数据库一致 ✅")
    print("=" * 78)
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
