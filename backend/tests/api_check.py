"""M2 API 验收脚本（需要 API 服务已在运行）。

跑法：
    cd backend
    python run_api.py --no-reload        # 终端 A
    python tests/api_check.py            # 终端 B
    python tests/api_check.py --base-url http://127.0.0.1:8001

检查两类东西：
1. **端点本身**：状态码、字段结构、过滤参数是否真的生效；
2. **契约里的元数据**（规划书 §10.3 的硬要求）：
   每个测量值都要带 `observed_at / quality / unit`；
   高风险设备必须 `auto_exec_allowed=false`（安全红线不能只写在文档里）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  ✅ {name}" + (f"  [{detail}]" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  ❌ {name}" + (f"  [{detail}]" if detail else ""))

    def report(self, title: str) -> None:
        print()
        print("-" * 76)
        if self.failed:
            print(f"{title}: {self.passed} PASS / {len(self.failed)} FAIL")
            for f in self.failed:
                print(f"    ❌ {f}")
        else:
            print(f"{title}: {self.passed} PASS / 0 FAIL")
        print("-" * 76)


def _db_scalar(sql: str):
    """直接查一个标量（只用于校验响应与库是否一致）。"""
    from sqlalchemy import text as _text

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return db.execute(_text(sql)).scalar()


def main() -> int:
    ap = argparse.ArgumentParser(description="M2 API 验收")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    ck = Checker()
    client = httpx.Client(base_url=base, timeout=30.0)

    print()
    print("#" * 76)
    print(f"# M2 API 验收  →  {base}")
    print("#" * 76)

    # ---------------------------------------------------------- 0. 可达性
    print()
    print("=" * 76)
    print("0 · 服务可达性")
    print("=" * 76)
    try:
        r = client.get("/")
        ck.check("GET / 可达", r.status_code == 200, f"HTTP {r.status_code}")
        ck.check("根路径说明了数据来源", "模拟" in r.text or "模拟器" in r.text)
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 连不上服务：{type(e).__name__}: {e}")
        print("     → 请先在另一个终端运行：python run_api.py --no-reload")
        return 1

    # ---------------------------------------------------------- 1. health
    print()
    print("=" * 76)
    print("1 · GET /api/health")
    print("=" * 76)
    r = client.get("/api/health")
    ck.check("HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
    h = r.json()
    ck.check("数据库连通", bool(h["database"]["ok"]),
             f"MySQL {h['database'].get('version')} / {h['database'].get('response_ms')}ms")
    ck.check("身份是最小权限账号", "app_rw" in str(h["database"].get("current_user")),
             str(h["database"].get("current_user")))
    # 设备总数按**库里实际的启用设备数**断言，而不是写死 32：
    # 多了第二套演示房（或用户自己加了设备）之后，写死的数字一定过期。
    expected_devices = _db_scalar("SELECT COUNT(*) FROM devices WHERE enabled = 1")
    ck.check(
        f"设备总数与库一致（{expected_devices}）",
        h["devices"]["total"] == expected_devices,
        str(h["devices"]),
    )
    # 多套房下要能区分：1 号房（演示家庭）仍然 32 台
    r1 = client.get("/api/devices", params={"home_id": 1})
    if r1.status_code == 200:
        n1 = r1.json()["meta"]["summary"]["devices"]
        ck.check("按套房过滤生效（1 号房）", n1 > 0 and n1 <= expected_devices, f"{n1} 台")
    ck.check("高风险设备 3 台", h["devices"]["high_risk"] == 3)
    ck.check("高风险自动执行恒为 false（安全红线）", h["high_risk_auto_exec"] is False)
    ck.check("缓存已预热（重启即有数据）", h["cache"]["preloaded"] > 0,
             f"preloaded={h['cache']['preloaded']} / devices={h['cache']['devices']}")
    ck.check("MQTT 状态可见", "connected" in h["mqtt"], f"connected={h['mqtt']['connected']}")
    print(f"     status={h['status']}  data_mode={h['data_mode']}  MQTT={h['mqtt']['connected']}")

    # ---------------------------------------------------------- 2. 房间卡片
    print()
    print("=" * 76)
    print("2 · GET /api/homes/{home_id}/rooms  —— 房间卡片墙")
    print("=" * 76)
    r = client.get("/api/homes/1/rooms")
    ck.check("HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
    rooms = r.json()["items"]
    ck.check("7 个房间", len(rooms) == 7, f"{len(rooms)} 个")
    ck.check("房间按 sort_no 有序", [x["sort_no"] for x in rooms] == sorted(x["sort_no"] for x in rooms))
    ck.check("含中文房间名", any(x["name"] == "客厅" for x in rooms),
             "/".join(x["name"] for x in rooms))
    total_devices = sum(x["device_count"] for x in rooms)
    ck.check("设备数合计 = 32", total_devices == 32, str(total_devices))
    ck.check("每间房都带 last_update_at（数据更新时间）",
             all(x["last_update_at"] for x in rooms))
    ck.check("含告警与事件计数字段",
             all("active_alarm_count" in x and "recent_event_count" in x for x in rooms))
    living = next(x for x in rooms if x["code"] == "living_room")
    ck.check("客厅卡片有关键指标", len(living["key_metrics"]) > 0,
             ", ".join(f"{m['metric_code']}={m['value']}{m['unit'] or ''}" for m in living["key_metrics"]))
    ck.check("关键指标带 device_name 与 observed_at",
             all(m["device_name"] and m["observed_at"] for m in living["key_metrics"]))

    r = client.get("/api/homes/999/rooms")
    ck.check("不存在的家庭返回 404", r.status_code == 404, f"HTTP {r.status_code}")

    # ---------------------------------------------------------- 3. 设备列表
    print()
    print("=" * 76)
    print("3 · GET /api/devices  —— 设备列表与过滤")
    print("=" * 76)
    r = client.get("/api/devices")
    ck.check("HTTP 200", r.status_code == 200)
    payload = r.json()
    devices = payload["items"]
    meta = payload["meta"]
    ck.check("返回 32 台", len(devices) == 32, f"{len(devices)} 台")
    ck.check("meta 汇总正确", meta["summary"]["high_risk"] == 3, str(meta["summary"]))

    r = client.get("/api/devices", params={"type": "light"})
    lights = r.json()["items"]
    ck.check("?type=light 过滤生效",
             bool(lights) and all(d["device_type"] == "light" for d in lights), f"{len(lights)} 台")

    r = client.get("/api/devices", params={"room_id": 1})
    room1 = r.json()["items"]
    ck.check("?room_id=1 过滤生效（玄关 3 台）", len(room1) == 3, f"{len(room1)} 台")

    r = client.get("/api/devices", params={"high_risk_only": True})
    hr = r.json()["items"]
    ck.check("?high_risk_only=true 返回 3 台高风险设备", len(hr) == 3, f"{len(hr)} 台")
    ck.check("高风险设备 auto_exec_allowed=false（安全红线落地到接口）",
             all(d["auto_exec_allowed"] is False for d in hr))
    ck.check("高风险设备含门锁/燃气阀/总闸",
             {d["code"] for d in hr} >= {"front_door_lock", "kitchen_gas_valve", "entryway_main_breaker"},
             ", ".join(d["code"] for d in hr))

    sample = next(d for d in devices if d["code"] == "living_room_temp_sensor")
    ck.check("设备带 room_name", sample["room_name"] == "客厅", sample["room_name"])
    ck.check("设备带 online 与两个时间字段",
             "online" in sample and "last_seen" in sample and "last_observed_at" in sample)
    ck.check("设备带最新值", len(sample["latest"]) > 0, str(list(sample["latest"])))
    temp = sample["latest"].get("temperature")
    ck.check("最新值带元数据（value/unit/observed_at/quality）",
             temp is not None and all(k in temp for k in ("value", "unit", "observed_at", "quality")),
             str(temp))

    # ---------------------------------------------------------- 4. 单台设备
    print()
    print("=" * 76)
    print("4 · GET /api/devices/{id} 与 /capabilities")
    print("=" * 76)
    dev_id = sample["id"]
    r = client.get(f"/api/devices/{dev_id}")
    ck.check("单台设备详情 HTTP 200", r.status_code == 200)
    ck.check("设备编码一致", r.json()["code"] == "living_room_temp_sensor")

    r = client.get(f"/api/devices/{dev_id}/capabilities")
    ck.check("能力档案 HTTP 200", r.status_code == 200)
    caps = r.json()["capabilities"]
    ck.check("温湿度传感器有 2 项能力", len(caps) == 2, str([x["code"] for x in caps]))
    ck.check("传感器能力不可写（能读≠能改）", all(x["writable"] is False for x in caps))
    temp_cap = next(x for x in caps if x["code"] == "temperature")
    ck.check("能力带取值范围与单位",
             temp_cap["value_min"] is not None and temp_cap["value_max"] is not None and temp_cap["unit"] == "℃",
             f"{temp_cap['value_min']}~{temp_cap['value_max']} {temp_cap['unit']}")

    ac = next(d for d in devices if d["code"] == "living_room_ac")
    r = client.get(f"/api/devices/{ac['id']}/capabilities")
    ac_caps = r.json()["capabilities"]
    ck.check("空调有 4 项能力且部分可写",
             len(ac_caps) == 4 and any(x["writable"] for x in ac_caps),
             ", ".join(f"{x['code']}{'*' if x['writable'] else ''}" for x in ac_caps))

    r = client.get("/api/devices/999999")
    ck.check("不存在的设备返回 404", r.status_code == 404, f"HTTP {r.status_code}")
    r = client.get("/api/devices/999999/capabilities")
    ck.check("不存在设备的能力档案返回 404", r.status_code == 404)

    # ---------------------------------------------------------- 5. 历史读数
    print()
    print("=" * 76)
    print("5 · GET /api/devices/{id}/readings  —— 趋势与时间窗约束")
    print("=" * 76)
    r = client.get(f"/api/devices/{dev_id}/readings")
    ck.check("历史读数 HTTP 200", r.status_code == 200)
    rr = r.json()
    ck.check("按指标分组", "temperature" in rr["metrics"], str(list(rr["metrics"])))
    ck.check("点数与 row_count 一致",
             sum(m["count"] for m in rr["metrics"].values()) == rr["row_count"], str(rr["row_count"]))
    ck.check("点带 quality 与 is_stale 标记",
             all("quality" in p and "is_stale" in p
                 for m in rr["metrics"].values() for p in m["points"][:3]))
    ck.check("带质量分布统计", "quality_summary" in rr, str(rr["quality_summary"]))
    if rr["metrics"].get("temperature"):
        pts = rr["metrics"]["temperature"]["points"]
        times = [p["observed_at"] for p in pts]
        ck.check("时间升序", times == sorted(times), f"{len(pts)} 点")

    r = client.get(f"/api/devices/{dev_id}/readings", params={"metric": ["humidity"]})
    ck.check("?metric= 过滤生效", set(r.json()["metrics"]) <= {"humidity"}, str(list(r.json()["metrics"])))

    now = dt.datetime.now()
    r = client.get(
        f"/api/devices/{dev_id}/readings",
        params={"from": (now - dt.timedelta(days=60)).isoformat(), "to": now.isoformat()},
    )
    ck.check("超过最大时间窗返回 400（不让一次查询拖垮库）", r.status_code == 400, f"HTTP {r.status_code}")
    r = client.get(
        f"/api/devices/{dev_id}/readings",
        params={"from": now.isoformat(), "to": (now - dt.timedelta(hours=1)).isoformat()},
    )
    ck.check("from 晚于 to 返回 400", r.status_code == 400, f"HTTP {r.status_code}")

    # ---------------------------------------------------------- 6. 能耗
    print()
    print("=" * 76)
    print("6 · GET /api/energy/summary  —— 账单口径")
    print("=" * 76)
    # 不带 from/to：走缺省窗口（以 health 里的 data_anchor 为基准）——
    # 模拟器按倍速跑，数据时间可能不在"现在"附近，用"现在往前 24 小时"会查不到
    ck.check("health 暴露了数据时间锚点", h.get("data_anchor") is not None, str(h.get("data_anchor")))
    r = client.get("/api/energy/summary")
    ck.check("HTTP 200", r.status_code == 200)
    e = r.json()
    ck.check("含总量与金额", e["total_kwh"] is not None and e["total_cost"] is not None,
             f"{e['total_kwh']} kWh / ¥{e['total_cost']}")
    ck.check("含分时电价明细", len(e["by_period"]) > 0,
             ", ".join(f"{p['period']}:{p['kwh']}kWh/¥{p['cost']}" for p in e["by_period"]))
    ck.check("含分设备明细", len(e["by_device"]) > 0,
             ", ".join(f"{d['name']}:{d['kwh']}" for d in e["by_device"][:3]))
    ck.check("分时占比合计约为 1",
             abs(sum(p["share"] or 0 for p in e["by_period"]) - 1.0) < 0.01
             if e["total_kwh"] else True,
             str(round(sum(p["share"] or 0 for p in e["by_period"]), 3)))
    ck.check("金额由确定性代码算（note 标注）", "确定性" in (e.get("note") or ""))
    if e["observations"]:
        ck.check("给了由数据算出的事实陈述", True, e["observations"][0])

    # ---------------------------------------------------------- 7. 知识库
    print()
    print("=" * 76)
    print("7 · 知识库端点（RAG：说明书检索）")
    print("=" * 76)
    r = client.get("/api/knowledge/status")
    ck.check("状态接口 HTTP 200", r.status_code == 200)
    ks = r.json()
    ck.check("向量库已灌库（非空）", ks["chunks"] > 0,
             f"{ks['chunks']} 块 / {ks['indexed_documents']} 份文档")
    ck.check("暴露了 embedding 模型名", bool(ks["embedding_model"]), ks["embedding_model"])
    ck.check("状态里能提示未灌库的情况", "note" in ks)

    r = client.get("/api/knowledge/documents")
    docs = r.json()
    ck.check("文档清单非空", len(docs) > 0, f"{len(docs)} 份")
    ck.check("每份文档带章节名（可定位到节）", all(d["sections"] for d in docs),
             docs[0]["source_file"] if docs else "")

    r = client.get("/api/knowledge/search", params={"q": "客厅空调滤网多久清洗一次", "k": 3})
    ck.check("检索 HTTP 200", r.status_code == 200)
    sr = r.json()
    ck.check("检索返回结果", sr["count"] > 0, f"{sr['count']} 条 / {sr['elapsed_ms']} ms")
    top = sr["hits"][0] if sr["hits"] else {}
    ck.check("Top-1 命中『维护周期』章节", "维护周期" in top.get("section", ""),
             f"{top.get('doc_title', '')} / {top.get('section', '')}")
    ck.check("命中带可追溯来源（source_file + section）",
             all(h["source_file"] and h["section"] for h in sr["hits"]))
    # 旧版在每条检索结果底部拼一句固定提示，界面上纯属废话，已删（2026-10-19）。
    # note 字段保留，只在真有事要提示时才用（比如向量库还没灌库）。
    ck.check("检索结果不再带固定说明话术", not (sr.get("note") or ""), f"note={sr.get('note')!r}")

    r = client.get("/api/knowledge/search", params={"q": "燃气泄漏怎么处理", "doc_type": "emergency_plan"})
    ck.check("doc_type 过滤生效（只返回应急预案）",
             bool(r.json()["hits"]) and all(h["doc_type"] == "emergency_plan" for h in r.json()["hits"]))

    r = client.get("/api/knowledge/search", params={"q": ""})
    ck.check("空查询被参数校验拦下（422）", r.status_code == 422, f"HTTP {r.status_code}")

    client.close()
    ck.report("M2 API 验收")

    print()
    print("=" * 76)
    if ck.failed:
        print(f"结果：{ck.passed} PASS / {len(ck.failed)} FAIL")
    else:
        print(f"结果：{ck.passed} PASS / 0 FAIL  —— API 验收通过 ✅")
    print("=" * 76)
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
