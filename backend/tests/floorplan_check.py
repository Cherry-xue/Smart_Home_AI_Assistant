"""户型图自定义验收：几何读写、校验、底图上传、重置。

跑法（后端必须在 8000 上）：
    python tests/floorplan_check.py

验收关注四类事
------------
1. **校验真的生效**：房间码必须存在于 rooms 表、不能重复、不能太小；
   底图必须是真图片（不是改个扩展名就行）。
2. **换底图会清空框选**：坐标系变了，保留旧框等于保留一堆错位的东西。
3. **画布尺寸必须与底图一致**：否则前端框选坐标和底图对不上。
4. **收尾要干净**：跑完恢复默认，别把测试户型留给用户。

⚠️ 脚本会**临时占用** `config/floorplan.json`，最后会恢复默认。
"""

from __future__ import annotations

import io
import sys

import requests
from PIL import Image

BASE = "http://127.0.0.1:8000"
ok = 0
bad = 0
failures: list[str] = []


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok, bad
    if cond:
        ok += 1
        print(f"  ✅ {name:<50}{extra}")
    else:
        bad += 1
        failures.append(name)
        print(f"  ❌ {name:<50}{extra}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * 78)


def png_bytes(w: int, h: int, color=(230, 235, 242)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# ============================================================================
section("一、默认户型")
# ============================================================================
r = requests.get(f"{BASE}/api/floorplan", timeout=30)
chk("GET /api/floorplan → 200", r.status_code == 200, f"HTTP {r.status_code}")
d = r.json()
chk("  初始为内置默认", d["source"] == "default", f"source={d['source']}")
chk("  8 个区域（含通道）", len(d["areas"]) == 8, f"{len(d['areas'])} 个")
cirq = [a for a in d["areas"] if a["is_circulation"]]
chk("  通道被标成交通空间", len(cirq) == 1 and cirq[0]["code"] == "hallway", str([a["code"] for a in cirq]))
chk("  房间名以数据库为准", all(a["name"] for a in d["areas"]))
chk("  没有未画出的房间", d["missing_rooms"] == [], str(d["missing_rooms"]))

# ============================================================================
section("二、保存框选")
# ============================================================================
base_areas = [
    {"code": "living_room", "x": 40, "y": 40, "w": 200, "h": 140},
    {"code": "kitchen", "x": 250, "y": 40, "w": 140, "h": 140},
    {"code": "master_bedroom", "x": 40, "y": 190, "w": 180, "h": 120},
    {"code": "hallway", "name": "通道", "x": 40, "y": 160, "w": 350, "h": 24, "is_circulation": True},
]
payload = {"view_box": {"x": 0, "y": 0, "w": 420, "h": 340}, "areas": base_areas}

r = requests.put(f"{BASE}/api/floorplan", json=payload, timeout=30)
chk("PUT /api/floorplan → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
saved = r.json()
chk("  source 变为 custom", saved["floorplan"]["source"] == "custom")
chk("  保存了 4 个区域", len(saved["floorplan"]["areas"]) == 4, f"{len(saved['floorplan']['areas'])}")
chk("  交通空间带 is_circulation", any(
    a["is_circulation"] for a in saved["floorplan"]["areas"]
))
chk("  missing_rooms 指出没画的房间", len(saved["floorplan"]["missing_rooms"]) == 4,
    str([x["name"] for x in saved["floorplan"]["missing_rooms"]]))
chk("  返回提示里说明了未画出的房间", "没有画到图上" in saved["message"], saved["message"][:60] + "…")

r2 = requests.get(f"{BASE}/api/floorplan", timeout=30)
chk("  重新 GET 仍是 custom（已落盘）", r2.json()["source"] == "custom")

# ============================================================================
section("二·五、一套房可以有多段走廊（编码不同即可）")
# ============================================================================
two_hall = {
    "view_box": payload["view_box"],
    "areas": [
        {"code": "living_room", "x": 40, "y": 40, "w": 200, "h": 100},
        {"code": "hallway_1", "name": "走廊 A", "x": 40, "y": 160, "w": 180, "h": 24, "is_circulation": True},
        {"code": "hallway_2", "name": "走廊 B", "x": 230, "y": 200, "w": 160, "h": 24, "is_circulation": True},
    ],
}
r = requests.put(f"{BASE}/api/floorplan", json=two_hall, timeout=30)
chk("两段走廊可同时保存 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:100]}")
if r.status_code == 200:
    got = r.json()["floorplan"]["areas"]
    circ = [a for a in got if a["is_circulation"]]
    chk("  两段都被标成交通空间", len(circ) == 2, str([a["code"] for a in circ]))
    chk("  两段编码不同", len({a["code"] for a in circ}) == 2, str([a["code"] for a in circ]))
    chk("  两段名字按输入保存", {a["name"] for a in circ} == {"走廊 A", "走廊 B"}, str([a["name"] for a in circ]))

# 同名走廊（两段都叫「走廊」）也应允许
two_hall["areas"][1]["name"] = "走廊"
two_hall["areas"][2]["name"] = "走廊"
r = requests.put(f"{BASE}/api/floorplan", json=two_hall, timeout=30)
chk("两段同名的走廊也可以 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:100]}")

# 恢复成第二节的布局，避免影响后面的用例
requests.put(f"{BASE}/api/floorplan", json=payload, timeout=30)

# ============================================================================
section("三、校验：不合法的必须拒绝")
# ============================================================================
bad_code = {
    "view_box": payload["view_box"],
    "areas": [{"code": "not_a_room", "x": 10, "y": 10, "w": 100, "h": 100}],
}
r = requests.put(f"{BASE}/api/floorplan", json=bad_code, timeout=30)
chk("不存在的房间码 → 400", r.status_code == 400, f"HTTP {r.status_code}")
chk("  错误里指出 fields", r.status_code == 400 and "fields" in str(r.json().get("detail")), str(r.json())[:70])

dup = {
    "view_box": payload["view_box"],
    "areas": [
        {"code": "living_room", "x": 10, "y": 10, "w": 100, "h": 100},
        {"code": "living_room", "x": 120, "y": 10, "w": 100, "h": 100},
    ],
}
r = requests.put(f"{BASE}/api/floorplan", json=dup, timeout=30)
chk("同一房间画两次 → 400", r.status_code == 400, f"HTTP {r.status_code}")

tiny = {"view_box": payload["view_box"], "areas": [{"code": "balcony", "x": 10, "y": 10, "w": 3, "h": 3}]}
r = requests.put(f"{BASE}/api/floorplan", json=tiny, timeout=30)
chk("框太小 → 400", r.status_code == 400, f"HTTP {r.status_code}")

# 空户型是合法的：删掉最后一个房间框并保存时要走这条路
r = requests.put(f"{BASE}/api/floorplan", json={"view_box": payload["view_box"], "areas": []}, timeout=30)
chk("空户型（0 个框）→ 200", r.status_code == 200, f"HTTP {r.status_code}")
chk("  区域数为 0", len(r.json()["floorplan"]["areas"]) == 0 if r.status_code == 200 else False)
# 恢复原布局
requests.put(f"{BASE}/api/floorplan", json=payload, timeout=30)

no_name = {
    "view_box": payload["view_box"],
    "areas": [{"code": "hallway", "x": 10, "y": 10, "w": 100, "h": 20, "is_circulation": True}],
}
r = requests.put(f"{BASE}/api/floorplan", json=no_name, timeout=30)
chk("交通空间没名字 → 400", r.status_code == 400, f"HTTP {r.status_code}")

# 同名房间（非走廊）必须拒绝：房间名唯一
dup_name = {
    "view_box": payload["view_box"],
    "areas": [
        {"code": "living_room", "name": "同一间", "x": 10, "y": 10, "w": 100, "h": 100},
        {"code": "kitchen", "name": "同一间", "x": 120, "y": 10, "w": 100, "h": 100},
    ],
}
r = requests.put(f"{BASE}/api/floorplan", json=dup_name, timeout=30)
chk("非走廊房间重名 → 400", r.status_code == 400, f"HTTP {r.status_code}")
chk("  错误里点名了重复的房间名", "同一间" in str(r.json().get("detail")), str(r.json())[:80])

# 走廊重名则允许
dup_circ = {
    "view_box": payload["view_box"],
    "areas": [
        {"code": "living_room", "x": 10, "y": 10, "w": 100, "h": 100},
        {"code": "hallway_1", "name": "走廊", "x": 10, "y": 160, "w": 100, "h": 24, "is_circulation": True},
        {"code": "hallway_2", "name": "走廊", "x": 10, "y": 200, "w": 100, "h": 24, "is_circulation": True},
    ],
}
r = requests.put(f"{BASE}/api/floorplan", json=dup_circ, timeout=30)
chk("走廊重名 → 200", r.status_code == 200, f"HTTP {r.status_code}")
requests.put(f"{BASE}/api/floorplan", json=payload, timeout=30)

circ_real = {
    "view_box": payload["view_box"],
    "areas": [{"code": "living_room", "x": 10, "y": 10, "w": 100, "h": 100, "is_circulation": True}],
}
r = requests.put(f"{BASE}/api/floorplan", json=circ_real, timeout=30)
chk("真实房间不能标成交通空间 → 400", r.status_code == 400, f"HTTP {r.status_code}")

# ============================================================================
section("四、上传底图")
# ============================================================================
img = png_bytes(640, 480)
r = requests.post(
    f"{BASE}/api/floorplan/image", files={"file": ("plan.png", img, "image/png")}, timeout=60
)
chk("上传 PNG → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
up = r.json()["floorplan"]
chk("  background 已设置", bool(up["background"]), str(up.get("background")))
chk("  画布尺寸 = 图片尺寸", (up["view_box"]["w"], up["view_box"]["h"]) == (640, 480),
    f"{up['view_box']['w']}×{up['view_box']['h']}")
chk("  换底图后框选被清空", len(up["areas"]) == 0, f"{len(up['areas'])} 个残留")
chk("  提示要求重新框选", "框" in r.json()["message"], r.json()["message"][:50] + "…")

r = requests.get(f"{BASE}/api/floorplan/image", timeout=30)
chk("GET /api/floorplan/image → 200 图片", r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n",
    f"HTTP {r.status_code}, {len(r.content)} 字节")

r = requests.post(
    f"{BASE}/api/floorplan/image",
    files={"file": ("fake.png", b"this is not an image", "image/png")},
    timeout=30,
)
chk("假图片（改扩展名）→ 400", r.status_code == 400, f"HTTP {r.status_code}")

r = requests.post(
    f"{BASE}/api/floorplan/image", files={"file": ("small.png", png_bytes(60, 60), "image/png")},
    timeout=30,
)
chk("图片太小 → 400", r.status_code == 400, f"HTTP {r.status_code}")

# ============================================================================
section("五、底图与画布必须对齐")
# ============================================================================
misaligned = {
    "view_box": {"x": 0, "y": 0, "w": 420, "h": 340},
    "areas": [{"code": "living_room", "x": 10, "y": 10, "w": 100, "h": 100}],
}
r = requests.put(f"{BASE}/api/floorplan", json=misaligned, timeout=30)
chk("画布尺寸与底图不一致 → 400", r.status_code == 400, f"HTTP {r.status_code}")

aligned = {
    "view_box": {"x": 0, "y": 0, "w": 640, "h": 480},
    "areas": [
        {"code": "living_room", "x": 40, "y": 40, "w": 260, "h": 180},
        {"code": "kitchen", "x": 320, "y": 40, "w": 180, "h": 180},
        {"code": "hallway", "name": "通道", "x": 40, "y": 240, "w": 460, "h": 30, "is_circulation": True},
    ],
}
r = requests.put(f"{BASE}/api/floorplan", json=aligned, timeout=30)
chk("对齐后保存 → 200", r.status_code == 200, f"HTTP {r.status_code}")
saved = r.json()["floorplan"]
chk("  底图仍在", bool(saved["background"]))
chk("  3 个区域已保存", len(saved["areas"]) == 3, f"{len(saved['areas'])}")

# ============================================================================
section("五·五、删除底图（只删图，框选与房间保留）")
# ============================================================================
r = requests.delete(f"{BASE}/api/floorplan/image", timeout=30)
chk("删除底图 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
if r.status_code == 200:
    g = r.json()["floorplan"]
    chk("  background 已清空", g["background"] is None, str(g.get("background")))
    # 这是与"换底图"最大的区别：换图清框，删图不清
    chk("  框选仍然保留（3 个）", len(g["areas"]) == 3, f"{len(g['areas'])}")
    chk(
        "  画布尺寸不变",
        (g["view_box"]["w"], g["view_box"]["h"]) == (640, 480),
        f"{g['view_box']['w']}×{g['view_box']['h']}",
    )
    chk("  提示说明框被保留", "保留" in r.json()["message"], r.json()["message"][:40])

r = requests.get(f"{BASE}/api/floorplan/image", timeout=30)
chk("删完再取底图 → 404", r.status_code == 404, f"HTTP {r.status_code}")

r = requests.delete(f"{BASE}/api/floorplan/image", timeout=30)
chk("重复删除 → 400（没有底图）", r.status_code == 400, f"HTTP {r.status_code}")

# 恢复现场，给第六节的"重置"用例用
requests.post(
    f"{BASE}/api/floorplan/image", files={"file": ("plan.png", img, "image/png")}, timeout=60
)
r = requests.put(f"{BASE}/api/floorplan", json=aligned, timeout=30)
chk("恢复底图与框选", r.status_code == 200, f"HTTP {r.status_code}")

# ============================================================================
section("六、重置")
# ============================================================================
r = requests.post(f"{BASE}/api/floorplan/reset", params={"keep_background": True}, timeout=30)
chk("只清框选 → 200", r.status_code == 200, f"HTTP {r.status_code}")
g = r.json()["floorplan"]
chk("  框选已清空", len(g["areas"]) == 0, f"{len(g['areas'])}")
chk("  底图保留", bool(g["background"]), str(g.get("background"))[:50])

r = requests.post(f"{BASE}/api/floorplan/reset", params={"keep_background": False}, timeout=30)
chk("恢复默认 → 200", r.status_code == 200, f"HTTP {r.status_code}")
g = r.json()["floorplan"]
chk("  source 回到 default", g["source"] == "default")
chk("  回到 8 个区域", len(g["areas"]) == 8, f"{len(g['areas'])}")
chk("  底图已删除", g["background"] is None)

r = requests.get(f"{BASE}/api/floorplan/image", timeout=30)
chk("  取底图 → 404", r.status_code == 404, f"HTTP {r.status_code}")

# ============================================================================
print("\n" + "=" * 78)
print(f"  结果：{ok} PASS / {bad} FAIL")
if failures:
    print("  失败项：")
    for f in failures:
        print(f"    · {f}")
print("=" * 78)
sys.exit(0 if bad == 0 else 1)
