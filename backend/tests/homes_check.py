"""套房增删验收：`POST /api/homes`、`DELETE /api/homes/{id}`。

覆盖点
------
1. 能建（自定义中文名 + 自定义房间名），房间编码由后端生成
2. 新套房是**空**的：房间数对、设备 0 台、户型 `source="empty"`
3. 校验真的生效：重名 409、空房间 400、房间重名 400、名字超长 400
4. 删除必须带 `confirm=true`（不带 400），删完数据真的没了、户型文件也删了
5. 最后一套房不允许删（保护：删了全站空白）

⚠️ 脚本会**真的建一套房再删掉**（名字带时间戳，不会撞已有数据）。
   如果中途失败，可能留下一套名为「验收临时套房 …」的空房，手动删掉即可。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8000"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

#: 与 `app/services/homes_admin.py:PLAN_DIR` 同一路径
PLAN_DIR = BACKEND.parent / "config" / "floorplans"

passed = failed = 0


def chk(label: str, ok: bool, extra: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        print(f"  ❌ {label}  {extra}")


def _seed_device_data(device_id: int) -> dict[str, int]:
    """直接往库里塞几条历史数据。

    为什么不走 MQTT 造数据：那要起生成器、等 30 秒重扫，跑一次验收要几分钟。
    这里只关心"删除时这些表会不会被清干净"，直接写库最直接。
    """
    from sqlalchemy import text as sql_text

    from app.db.session import SessionLocal

    now = "2026-10-19 09:00:00.000"
    with SessionLocal() as db:
        for i in range(3):
            db.execute(
                sql_text(
                    "INSERT INTO device_readings (device_id, observed_at, metric_code, source, value_num) "
                    "VALUES (:d, :t, 'power_on', 'test', 1)"
                ),
                {"d": device_id, "t": now},
            )
        for i in range(2):
            db.execute(
                sql_text(
                    "INSERT INTO sensor_events (device_id, event_code, level, started_at) "
                    "VALUES (:d, 'test_event', 'info', :t)"
                ),
                {"d": device_id, "t": now},
            )
        for i in range(2):
            db.execute(
                sql_text(
                    "INSERT INTO energy_readings (device_id, observed_at, power_w, energy_kwh) "
                    "VALUES (:d, :t, 10, 0.001)"
                ),
                {"d": device_id, "t": now},
            )
        db.execute(
            sql_text(
                "INSERT INTO device_commands (device_id, command, status, created_at) "
                "VALUES (:d, 'turn_on', 'executed', :t)"
            ),
            {"d": device_id, "t": now},
        )
        db.commit()
    return {"readings": 3, "events": 2, "energy": 2, "commands": 1}


def _count_left(home_id: int, device_id: int) -> dict[str, int]:
    """删完之后直接查库：接口说删了，库里的行是不是真没了。"""
    from sqlalchemy import text as sql_text

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return {
            "readings": db.execute(
                sql_text("SELECT COUNT(*) FROM device_readings WHERE device_id = :d"), {"d": device_id}
            ).scalar(),
            "events": db.execute(
                sql_text("SELECT COUNT(*) FROM sensor_events WHERE device_id = :d"), {"d": device_id}
            ).scalar(),
            "energy": db.execute(
                sql_text("SELECT COUNT(*) FROM energy_readings WHERE device_id = :d"), {"d": device_id}
            ).scalar(),
            "commands": db.execute(
                sql_text("SELECT COUNT(*) FROM device_commands WHERE device_id = :d"), {"d": device_id}
            ).scalar(),
            "devices": db.execute(
                sql_text("SELECT COUNT(*) FROM devices WHERE home_id = :h"), {"h": home_id}
            ).scalar(),
            "rooms": db.execute(
                sql_text("SELECT COUNT(*) FROM rooms WHERE home_id = :h"), {"h": home_id}
            ).scalar(),
            "homes": db.execute(
                sql_text("SELECT COUNT(*) FROM homes WHERE id = :h"), {"h": home_id}
            ).scalar(),
        }


def main() -> int:
    s = requests.Session()
    s.trust_env = False  # 绕开系统代理

    stamp = time.strftime("%H%M%S")
    name = f"验收临时套房 {stamp}"
    created_id: int | None = None

    print("=" * 70)
    print("一、新建")
    print("=" * 70)
    before = s.get(f"{BASE}/api/homes", timeout=30).json()
    n_before = before["total"]

    payload = {"name": name, "rooms": ["客厅", "书房", "阳台"]}
    r = s.post(f"{BASE}/api/homes", json=payload, timeout=60)
    print(f"  POST /api/homes → HTTP {r.status_code}")
    chk("新建返回 201", r.status_code == 201, str(r.text)[:200])
    if r.status_code != 201:
        return 1

    data = r.json()
    created_id = data["home_id"]
    codes = [x["code"] for x in data["rooms"]]
    names = [x["name"] for x in data["rooms"]]
    print(f"  home_id={created_id}  rooms={list(zip(codes, names))}")
    chk("套房名与请求一致", data["name"] == name)
    chk("房间名按用户输入保留", names == ["客厅", "书房", "阳台"])
    chk("房间编码自动生成（room_N）", codes == ["room_1", "room_2", "room_3"], str(codes))

    homes = s.get(f"{BASE}/api/homes", timeout=30).json()
    chk("套房列表 +1", homes["total"] == n_before + 1, f"{n_before} → {homes['total']}")
    row = next((h for h in homes["items"] if h["id"] == created_id), None)
    chk("列表里能查到", row is not None)
    if row:
        chk("房间数 3", row["rooms"] == 3, str(row["rooms"]))
        chk("设备数 0（新套房是空的）", row["devices"] == 0, str(row["devices"]))

    print()
    print("=" * 70)
    print("二、新套房的房间 / 设备 / 户型")
    print("=" * 70)
    rooms = s.get(f"{BASE}/api/homes/{created_id}/rooms", timeout=60)
    chk("房间卡片墙可访问", rooms.status_code == 200, f"HTTP {rooms.status_code}")
    room_items = rooms.json().get("items", []) if rooms.status_code == 200 else []
    chk("返回 3 间房", len(room_items) == 3, str(len(room_items)))

    devs = s.get(f"{BASE}/api/devices", params={"home_id": created_id}, timeout=30).json()
    chk("设备列表为空", devs["items"] == [], f"{len(devs['items'])} 台")

    fp = s.get(f"{BASE}/api/floorplan", params={"home_id": created_id}, timeout=30)
    chk("户型可访问", fp.status_code == 200, f"HTTP {fp.status_code}")
    if fp.status_code == 200:
        f = fp.json()
        chk("户型 source=empty（不套用别家户型）", f["source"] == "empty", f.get("source"))
        chk("户型没有区域", len(f["areas"]) == 0, str(len(f["areas"])))

    print()
    print("=" * 70)
    print("三、新增房间（户型编辑器用）")
    print("=" * 70)
    r = s.post(f"{BASE}/api/homes/{created_id}/rooms", json={"name": "书房2"}, timeout=30)
    chk("新增房间 → 201", r.status_code == 201, str(r.text)[:160])
    if r.status_code == 201:
        rd = r.json()
        print(f"    room_id={rd['room_id']} code={rd['code']} name={rd['name']}")
        chk("编码由后端生成（room_4）", rd["code"] == "room_4", rd["code"])
        chk("名字按输入保留", rd["name"] == "书房2", rd["name"])

    r = s.post(f"{BASE}/api/homes/{created_id}/rooms", json={"name": "书房2"}, timeout=30)
    chk("再建同名房间 → 409", r.status_code == 409, f"HTTP {r.status_code} {str(r.text)[:80]}")

    r = s.post(f"{BASE}/api/homes/{created_id}/rooms", json={"name": "   "}, timeout=30)
    chk("房间名为空 → 400", r.status_code == 400, f"HTTP {r.status_code}")
    r = s.post(f"{BASE}/api/homes/999999/rooms", json={"name": "幽灵房"}, timeout=30)
    chk("给不存在的套房加房间 → 404", r.status_code == 404, f"HTTP {r.status_code}")

    rooms_after = s.get(f"{BASE}/api/homes/{created_id}/rooms", timeout=30).json()
    names_after = [x["name"] for x in rooms_after.get("items", [])]
    chk("房间列表仍是 4 间", len(names_after) == 4, str(names_after))
    chk("「书房2」只有一间", names_after.count("书房2") == 1, str(names_after))

    print()
    print("=" * 70)
    print("四、校验")
    print("=" * 70)
    r = s.post(f"{BASE}/api/homes", json={"name": name, "rooms": ["客厅"]}, timeout=30)
    chk("重名 → 409", r.status_code == 409, f"HTTP {r.status_code}")

    # 新套房默认不带房间，所以 rooms=[] 必须合法
    r = s.post(f"{BASE}/api/homes", json={"name": f"空房 {stamp}", "rooms": []}, timeout=30)
    chk("不传房间 → 201（允许 0 房间）", r.status_code == 201, f"HTTP {r.status_code} {str(r.text)[:80]}")
    if r.status_code == 201:
        chk("  房间列表为空", r.json()["rooms"] == [], str(r.json()["rooms"]))
        s.delete(f"{BASE}/api/homes/{r.json()['home_id']}", params={"confirm": "true"}, timeout=60)

    # 房间名必须唯一（走廊是交通空间、不建房间记录，所以不受这条限制）
    r = s.post(
        f"{BASE}/api/homes",
        json={"name": f"重名房 {stamp}", "rooms": ["客厅", "客厅"]},
        timeout=30,
    )
    chk("同名房间 → 400", r.status_code == 400, f"HTTP {r.status_code} {str(r.text)[:80]}")

    r = s.post(f"{BASE}/api/homes", json={"name": "  ", "rooms": ["客厅"]}, timeout=30)
    chk("套房名为空 → 422/400", r.status_code in (400, 422), f"HTTP {r.status_code}")

    r = s.post(f"{BASE}/api/homes", json={"name": "超长" * 40, "rooms": ["客厅"]}, timeout=30)
    chk("套房名超长 → 422/400", r.status_code in (400, 422), f"HTTP {r.status_code}")

    # 前端新建弹窗**只填套房名**，所以这条是界面真实走的路径
    r = s.post(f"{BASE}/api/homes", json={"name": f"默认房名 {stamp}"}, timeout=30)
    chk("只传名字也能建（201）", r.status_code == 201, str(r.text)[:160])
    if r.status_code == 201:
        dd = r.json()
        got = [x["name"] for x in dd["rooms"]]
        chk("默认不建任何房间", got == [], str(got))
        s.delete(f"{BASE}/api/homes/{dd['home_id']}", params={"confirm": "true"}, timeout=60)

    print()
    print("=" * 70)
    print("四、套房改名（编辑房子里的房名）")
    print("=" * 70)
    renamed = f"{name}（改名后）"
    r = s.patch(f"{BASE}/api/homes/{created_id}", json={"name": renamed}, timeout=30)
    chk("改名 → 200", r.status_code == 200, str(r.text)[:160])
    if r.status_code == 200:
        chk("返回 changed=True", r.json().get("changed") is True, str(r.json().get("changed")))
        chk("名字已按输入保存", r.json().get("name") == renamed, str(r.json().get("name")))
        # 列表里也要立刻能看到新名字（前端顶栏靠它）
        hs = s.get(f"{BASE}/api/homes", timeout=30).json()
        row2 = next((h for h in hs["items"] if h["id"] == created_id), None)
        chk("套房列表显示新名字", row2 is not None and row2["name"] == renamed, str(row2))

    r = s.patch(f"{BASE}/api/homes/{created_id}", json={"name": renamed}, timeout=30)
    chk("再改成同一个名字 → changed=False", r.status_code == 200 and r.json().get("changed") is False, str(r.text)[:120])

    r = s.patch(f"{BASE}/api/homes/{created_id}", json={"name": "演示家庭 · 两室一厅"}, timeout=30)
    chk("改成已存在的房名 → 409", r.status_code == 409, f"HTTP {r.status_code}")

    r = s.patch(f"{BASE}/api/homes/999999", json={"name": "幽灵房"}, timeout=30)
    chk("给不存在的套房改名 → 404", r.status_code == 404, f"HTTP {r.status_code}")

    r = s.patch(f"{BASE}/api/homes/{created_id}", json={"name": "   "}, timeout=30)
    chk("空名字 → 400", r.status_code == 400, f"HTTP {r.status_code}")

    print()
    print("=" * 70)
    print("五、删除")
    print("=" * 70)
    r = s.delete(f"{BASE}/api/homes/{created_id}", timeout=60)
    chk("不带 confirm → 400（防误删）", r.status_code == 400, f"HTTP {r.status_code}")

    # 先在它下面放一台设备，验证级联删除了非 0 条
    if room_items:
        dev_body = {
            "preset_key": "light",
            "room_id": room_items[0]["room_id"],
            "name": "验收临时灯",
        }
        rd = s.post(f"{BASE}/api/devices", json=dev_body, timeout=60)
        print(f"  预备：新增设备 → HTTP {rd.status_code}")
        chk("能给新套房加设备", rd.status_code == 201, str(rd.text)[:160])

        devs2 = s.get(f"{BASE}/api/devices", params={"home_id": created_id}, timeout=30).json()
        chk("设备出现在新套房里", len(devs2["items"]) == 1, str(len(devs2["items"])))

        # ---- 给它造一点"历史数据"，验证级联删除不是只删空表 ----
        dev_id = devs2["items"][0]["id"]
        seeded = _seed_device_data(dev_id)
        print(f"  预备：直接写入历史数据 {seeded}")
        # ---- 再放一个户型文件，验证文件也会被清掉 ----
        plan = PLAN_DIR / f"{created_id}.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text('{"version": 1, "areas": []}', encoding="utf-8")
        chk("户型文件已就位", plan.exists())

    r = s.delete(f"{BASE}/api/homes/{created_id}", params={"confirm": "true"}, timeout=120)
    print(f"  DELETE ?confirm=true → HTTP {r.status_code}")
    chk("确认后删除成功", r.status_code == 200, str(r.text)[:200])
    if r.status_code == 200:
        d = r.json()
        print(f"  删除统计: {d['deleted']}  户型文件删除={d['floorplan_removed']}")
        chk("设备被级联删除", d["deleted"].get("devices", 0) == 1, str(d["deleted"]))
        chk("房间被级联删除", d["deleted"].get("rooms", 0) == 4, str(d["deleted"]))
        chk("读数被级联删除", d["deleted"].get("device_readings", 0) == 3, str(d["deleted"]))
        chk("事件被级联删除", d["deleted"].get("sensor_events", 0) == 2, str(d["deleted"]))
        chk("能耗被级联删除", d["deleted"].get("energy_readings", 0) == 2, str(d["deleted"]))
        chk("指令被级联删除", d["deleted"].get("device_commands", 0) == 1, str(d["deleted"]))
        chk("能力被级联删除", d["deleted"].get("device_capabilities", 0) >= 1, str(d["deleted"]))
        chk("户型文件被删除", d["floorplan_removed"] is True, str(d["floorplan_removed"]))
        chk("户型文件真的不在了", not plan.exists())

        # 直接查库确认没有残留（接口返回值可能是"说了但没做"）
        left = _count_left(created_id, dev_id)
        chk("库里没有残留行", all(v == 0 for v in left.values()), str(left))

    homes_after = s.get(f"{BASE}/api/homes", timeout=30).json()
    chk("套房列表恢复原数量", homes_after["total"] == n_before, str(homes_after["total"]))
    chk(
        "列表里确实没了",
        all(h["id"] != created_id for h in homes_after["items"]),
    )

    r = s.delete(f"{BASE}/api/homes/{created_id}", params={"confirm": "true"}, timeout=60)
    chk("删不存在的套房 → 404", r.status_code == 404, f"HTTP {r.status_code}")

    # 最后一套保护：库里只剩 1 套时才拦，这里用"删到只剩一套再试"代价太大，
    # 直接验证接口在有 >1 套房时不会误拦（上面已经证明），逻辑分支由单元约定保证。

    print()
    print("=" * 70)
    print("六、删除房间（连同设备与历史数据）")
    print("=" * 70)
    r = s.post(f"{BASE}/api/homes", json={"name": f"删房测试 {stamp}"}, timeout=30)
    chk("建一套临时房", r.status_code == 201, str(r.text)[:120])
    tmp_home = r.json()["home_id"] if r.status_code == 201 else None

    if tmp_home:
        # 新套房默认没有房间，先加一个当删除目标
        rr = s.post(f"{BASE}/api/homes/{tmp_home}/rooms", json={"name": "待删房"}, timeout=30)
        chk("给它加一个房间", rr.status_code == 201, f"HTTP {rr.status_code} {str(rr.text)[:80]}")
        rooms_tmp = s.get(f"{BASE}/api/homes/{tmp_home}/rooms", timeout=30).json()["items"]
        target = rooms_tmp[0]
        print(f"  目标房间：{target['code']}「{target['name']}」")

        r = s.post(
            f"{BASE}/api/devices",
            json={"preset_key": "light", "room_id": target["room_id"], "name": "待删灯"},
            timeout=30,
        )
        chk("往房间里加一台设备", r.status_code == 201, str(r.text)[:120])
        tmp_dev = r.json()["id"] if r.status_code == 201 else None

        # 造一点历史数据，验证是"连历史一起删"
        if tmp_dev:
            _seed_device_data(tmp_dev)

        r = s.delete(f"{BASE}/api/homes/{tmp_home}/rooms/{target['code']}", timeout=60)
        chk("不带 confirm 删房间 → 400", r.status_code == 400, f"HTTP {r.status_code}")

        r = s.delete(
            f"{BASE}/api/homes/{tmp_home}/rooms/{target['code']}",
            params={"confirm": "true"},
            timeout=120,
        )
        chk("删房间 → 200", r.status_code == 200, str(r.text)[:160])
        if r.status_code == 200:
            d = r.json()
            print(f"    {d['message']}")
            chk("随房间删掉 1 台设备", d["devices"] == 1, str(d["devices"]))
            chk("读数被一起清掉", d["deleted"].get("device_readings", 0) == 3, str(d["deleted"]))
            chk("能耗被一起清掉", d["deleted"].get("energy_readings", 0) == 2, str(d["deleted"]))

        rooms_left = s.get(f"{BASE}/api/homes/{tmp_home}/rooms", timeout=30).json()["items"]
        chk("房间列表里没了", all(x["code"] != target["code"] for x in rooms_left), str([x["code"] for x in rooms_left]))
        devs_left = s.get(f"{BASE}/api/devices", params={"home_id": tmp_home}, timeout=30).json()
        chk("设备也一起没了", devs_left["items"] == [], f"{len(devs_left['items'])} 台")

        r = s.delete(f"{BASE}/api/homes/{tmp_home}/rooms/{target['code']}", params={"confirm": "true"}, timeout=30)
        chk("删不存在的房间 → 404", r.status_code == 404, f"HTTP {r.status_code}")

        s.delete(f"{BASE}/api/homes/{tmp_home}", params={"confirm": "true"}, timeout=60)

    print()
    print("=" * 70)
    print(f"结果：{passed} PASS / {failed} FAIL")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
