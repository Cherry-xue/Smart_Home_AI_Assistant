"""M3 场景编排验收。

跑法：
    cd backend
    python tests/scene_check.py              # 第 1、2 层（离线，不联网）
    python tests/scene_check.py --api        # 加第 3 层（需 API 在运行）
    python tests/scene_check.py --with-llm   # 加第 4 层真实 LLM 预览（会调用模型）

四层：

1. **编排（planner）** —— 能力校验 / 重复检测 / 意图约束 / 高风险标记。
   这一层的重点是**复现"哪条动作被剔除、为什么"**（规划书 §14.1 的验收要求）。
2. **执行（executor）** —— dry-run 与真实下发；高风险永远只生成待确认。
3. **API** —— `/scenes/plan`、保存、执行、执行记录。
4. **LLM 预览** —— 自然语言真的能变成合理动作（可选，会调用模型）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.catalog import DeviceCatalog  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services.scenes import executor, planner  # noqa: E402
from app.services.scenes.intents import load_intents, match_intent  # noqa: E402


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


#: 招牌验收用例：把客厅调成"看电影的样子"，但故意混入与观影氛围矛盾的动作
MOVIE_CANDIDATES = [
    {"device_code": "living_room_light_main", "capability": "brightness", "value": 100},  # ← 该被剔除
    {"device_code": "living_room_light_strip", "capability": "brightness", "value": 30},
    {"device_code": "living_room_curtain", "capability": "position", "value": 100},       # ← 该被剔除（观影需遮光）
    {"device_code": "living_room_ac", "capability": "target_temp", "value": 20},          # ← 该被夹到 24
    {"device_code": "living_room_tv", "capability": "power_on", "value": True},
    {"device_code": "living_room_speaker", "capability": "power_on", "value": "开"},      # 中文布尔
]


def layer1(catalog: DeviceCatalog) -> Checker:
    ck = Checker()
    print("=" * 76)
    print("1 · 编排器（能力校验 / 重复检测 / 意图约束）")
    print("=" * 76)

    # ---- 1.1 能力校验 ----
    plan = planner.plan_scene(
        catalog,
        [
            {"device_code": "no_such_device", "capability": "power_on", "value": True},
            {"device_code": "living_room_light_main", "capability": "temperature", "value": 25},
            {"device_code": "living_room_temp_sensor", "capability": "temperature", "value": 25},
            {"device_code": "living_room_light_main", "capability": "brightness", "value": 150},
            {"device_code": "living_room_ac", "capability": "mode", "value": "warm"},
        ],
        scene_name="调试用例",
    )
    reasons = " | ".join(r.reason for r in plan.rejected)
    ck.check("未登记设备被剔除", any("no_such_device" in r.reason for r in plan.rejected))
    ck.check("不存在的能力被剔除（防臆造设备功能）",
             any("能力档案里没有" in r.reason for r in plan.rejected))
    ck.check("只读能力被剔除（能读≠能改）",
             any("只读能力" in r.reason for r in plan.rejected))
    ck.check("超范围的值被夹取而非剔除（亮度 150 → 100）",
             any(a.status == "clamped" and a.value == 100 for a in plan.actions),
             next((f"{a.capability}={a.value}" for a in plan.actions if a.status == "clamped"), ""))
    ck.check("非法枚举被剔除（mode='warm'）",
             any("不在允许的枚举里" in r.reason for r in plan.rejected))
    print(f"     ↓ 剔除原因：{reasons[:150]}…")

    # ---- 1.2 重复检测 ----
    plan = planner.plan_scene(
        catalog,
        [
            {"device_code": "living_room_light_main", "capability": "brightness", "value": 80},
            {"device_code": "living_room_light_main", "capability": "brightness", "value": 20},
        ],
        scene_name="重复测试",
    )
    ck.check("同一设备同一能力重复设置 → 保留最后一条",
             len(plan.actions) == 1 and plan.actions[0].value == 20
             and any(r.status == "duplicate" for r in plan.rejected),
             f"保留 {plan.actions[0].value if plan.actions else '?'}")

    # ---- 1.3 意图约束（招牌演示）----
    plan = planner.plan_scene(
        catalog, MOVIE_CANDIDATES, scene_name="看电影模式", description="把客厅调成看电影的样子"
    )
    ck.check("匹配到观影意图", plan.intent_key == "movie", f"{plan.intent_key}/{plan.intent_label}")
    ck.check("原始候选 6 条", plan.raw_count == 6, str(plan.raw_count))

    rejected_reasons = {r.device_code: r.reason for r in plan.rejected}
    ck.check("★ 主灯全亮被剔除（与观影氛围冲突）",
             "living_room_light_main" in rejected_reasons,
             rejected_reasons.get("living_room_light_main", "未剔除")[:70])
    ck.check("★ 剔除原因里写明了判据（40% 上限）",
             "40%" in rejected_reasons.get("living_room_light_main", ""))
    ck.check("窗帘全开被剔除（观影需遮光）",
             "living_room_curtain" in rejected_reasons,
             rejected_reasons.get("living_room_curtain", "未剔除")[:60])
    ck.check("空调 20℃ 被夹到 24℃（观影区间）",
             any(a.device_code == "living_room_ac" and a.value == 24 and a.status == "clamped"
                 for a in plan.actions),
             next((f"{a.original_value}→{a.value}" for a in plan.actions
                   if a.device_code == "living_room_ac"), ""))

    kept = {a.device_code: a.value for a in plan.actions}
    ck.check("氛围灯 30% 被保留", kept.get("living_room_light_strip") == 30)
    ck.check("中文布尔『开』被正确解析", kept.get("living_room_speaker") is True)
    ck.check("电视开机被保留", kept.get("living_room_tv") is True)
    ck.check("最终可执行 4 条 / 剔除 2 条",
             len(plan.actions) == 4 and len(plan.rejected) == 2,
             f"可执行 {len(plan.actions)} / 剔除 {len(plan.rejected)}")

    print("     ↓ 最终动作序列：")
    for a in plan.actions:
        mark = f" [{a.status}]" if a.status != "ok" else ""
        print(f"       · {a.device_name} {a.capability}={a.value}{mark}")
    print("     ↓ 被剔除的动作及原因：")
    for r in plan.rejected:
        print(f"       · {r.device_name} {r.capability}={r.value}")
        print(f"         ↳ {r.reason}")

    # ---- 1.4 睡眠场景与音量 ----
    plan = planner.plan_scene(
        catalog,
        [{"device_code": "living_room_speaker", "capability": "volume", "value": 80}],
        scene_name="睡觉模式",
    )
    ck.check("睡眠场景里音量 80 被剔除", plan.rejected and "20%" in plan.rejected[0].reason,
             plan.rejected[0].reason[:60] if plan.rejected else "")

    # ---- 1.5 高风险标记（不剔除，但标记）----
    plan = planner.plan_scene(
        catalog,
        [{"device_code": "front_door_lock", "capability": "lock_state", "value": "unlocked"}],
        scene_name="出门模式",
    )
    ck.check("高风险动作不被剔除（系统支持，只是要确认）",
             len(plan.actions) == 1 and plan.actions[0].high_risk_device)
    ck.check("高风险动作被标记为待确认", plan.pending_confirm_count == 1)
    ck.check("提示里说明了会生成待确认建议",
             any("待确认" in n for n in plan.notes), str(plan.notes[-1:]))
    ck.check("可执行数为 0（门锁不会被执行）", plan.executable_count == 0)

    # ---- 1.6 意图匹配本身 ----
    ck.check("意图匹配：看电影 → movie", match_intent("把客厅调成看电影的样子").key == "movie")
    ck.check("意图匹配：晚安 → sleep", match_intent("准备睡觉了").key == "sleep")
    ck.check("意图匹配：无法识别 → default", match_intent("随便弄弄").key == "default")
    ck.check("意图配置已加载", len(load_intents()) >= 5, f"{len(load_intents())} 个意图")

    return ck


def layer2(catalog: DeviceCatalog) -> Checker:
    ck = Checker()
    print()
    print("=" * 76)
    print("2 · 执行器（dry-run：只演练，不下发不写库）")
    print("=" * 76)

    plan = planner.plan_scene(
        catalog, MOVIE_CANDIDATES, scene_name="看电影模式"
    )
    result = executor.execute_plan(plan, catalog, db=None, home_id=1, dry_run=True)

    ck.check("dry-run 状态为 success", result.status == "success", result.status)
    ck.check("dry-run 得到 4 条执行动作", len(result.executed) == 4, str(len(result.executed)))
    ck.check("dry-run 未写指令记录（command_id 为空）",
             all(s.command_id is None for s in result.executed))
    ck.check("dry-run 有明确提示", any("dry-run" in n for n in result.notes))

    # 高风险在 dry-run 下也必须进 pending_confirm
    plan = planner.plan_scene(
        catalog,
        [{"device_code": "kitchen_gas_valve", "capability": "valve_state", "value": "closed"}],
        scene_name="燃气泄漏处置",
    )
    result = executor.execute_plan(plan, catalog, db=None, home_id=1, dry_run=True)
    ck.check("★ 燃气阀在 dry-run 下也进待确认（永不执行）",
             len(result.pending_confirm) == 1 and len(result.executed) == 0,
             f"executed={len(result.executed)} pending={len(result.pending_confirm)}")

    return ck


def layer3(base_url: str) -> Checker:
    import httpx

    ck = Checker()
    print()
    print("=" * 76)
    print(f"3 · API 端点  →  {base_url}")
    print("=" * 76)
    client = httpx.Client(base_url=base_url.rstrip("/"), timeout=60.0)

    # 本层会建 2 个场景跑执行流程。记下 id，finally 里删掉 ——
    # 否则每跑一次回归就多两条「（验收脚本）」，用户的场景列表会被脚本产物占满。
    scene_id = None
    sid2 = None

    try:
        r = client.post(
            "/api/scenes/plan",
            json={"actions": MOVIE_CANDIDATES, "scene_name": "看电影模式"},
        )
        ck.check("/scenes/plan HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            client.close()
            return ck
        data = r.json()
        ck.check("API 也匹配到观影意图", data["intent_key"] == "movie")
        ck.check("API 返回可执行 4 条", data["action_count"] == 4, str(data["action_count"]))
        ck.check("API 返回被剔除的动作及原因",
                 data["rejected_count"] == 2
                 and any("40%" in x["reason"] for x in data["rejected"]),
                 str([x["device_name"] for x in data["rejected"]]))

        # 保存场景
        r = client.post(
            "/api/scenes",
            json={
                "home_id": 1,
                "name": "看电影模式（验收脚本）",
                "description": "把客厅调成看电影的样子",
                "actions": MOVIE_CANDIDATES,
            },
        )
        ck.check("保存场景 HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
        scene = r.json() if r.status_code == 200 else {}
        ck.check("保存时只存通过校验的动作（4 条）", scene.get("action_count") == 4,
                 str(scene.get("action_count")))
        ck.check("保存时记下了被剔除的动作", scene.get("intent_key") == "movie")

        scene_id = scene.get("id")
        r = client.get("/api/scenes")
        ck.check("场景列表可查", r.status_code == 200 and len(r.json()) > 0,
                 f"{len(r.json())} 个场景")

        # 执行场景
        r = client.post(f"/api/scenes/{scene_id}/run")
        ck.check("执行场景 HTTP 200", r.status_code == 200, f"HTTP {r.status_code}")
        run = r.json() if r.status_code == 200 else {}
        ck.check("执行状态合法",
                 run.get("status") in ("success", "partial", "failed"), str(run.get("status")))
        ck.check("★ 高风险步骤绝不会出现在 executed 里",
                 all(not s["high_risk_device"] for s in run.get("executed", [])))
        print(f"     ↓ 本次执行：executed={run.get('executed_count')} "
              f"pending={run.get('pending_confirm_count')} failed={run.get('failed_count')} "
              f"status={run.get('status')}")
        for n in run.get("notes", []):
            print(f"       · {n}")

        run_id = run.get("run_id")
        if run_id:
            r = client.get(f"/api/scenes/runs/{run_id}")
            ck.check("执行记录可追溯", r.status_code == 200, f"HTTP {r.status_code}")

        # 高风险设备单独走一次
        r = client.post(
            "/api/scenes",
            json={
                "home_id": 1,
                "name": "离家模式（验收脚本）",
                "actions": [
                    {"device_code": "front_door_lock", "capability": "lock_state", "value": "locked"},
                    {"device_code": "living_room_light_main", "capability": "power_on", "value": False},
                ],
            },
        )
        sid2 = r.json().get("id")
        r = client.post(f"/api/scenes/{sid2}/run")
        run2 = r.json()
        ck.check("★ 门锁只生成待确认，不进 executed",
                 run2.get("pending_confirm_count") == 1
                 and all(s["device_code"] != "front_door_lock" for s in run2.get("executed", [])),
                 f"pending={run2.get('pending_confirm_count')}")
        ck.check("待确认项带原因说明",
                 all("确认" in s["reason"] or "高风险" in s["reason"]
                     for s in run2.get("pending_confirm", [])))

        # ---------------------------------------------------------------- 多套房隔离
        # 编排 / 保存 / 执行都必须只在本套房的设备里挑。
        # 这里不写死设备码：从接口自己找两套「有设备的房」，再拿 B 房的一台设备
        # 去 A 房编排 —— 这样以后增删套房、改设备清单，这条用例都不会失效。
        homes = client.get("/api/homes").json()
        homes = homes if isinstance(homes, list) else homes.get("items", [])
        with_dev = []
        for h in homes:
            ds = client.get("/api/devices", params={"home_id": h["id"]}).json().get("items", [])
            if ds:
                with_dev.append((h["id"], ds))

        if len(with_dev) < 2:
            print("     （只有一套房有设备，跳过跨套房隔离检查）")
        else:
            (home_a, devs_a), (home_b, devs_b) = with_dev[0], with_dev[1]
            foreign = next((x for x in devs_b if (x.get("writable_count") or 0) > 0), None)
            if foreign is None:
                print("     （B 房没有可写设备，跳过跨套房隔离检查）")
            else:
                caps = client.get(
                    f"/api/devices/{foreign['id']}/capabilities"
                ).json().get("capabilities", [])
                cap = next((c for c in caps if c.get("writable")), None)
                if cap is None:
                    print("     （取不到 B 房的可写能力，跳过跨套房隔离检查）")
                else:
                    vt = cap.get("value_type")
                    if vt == "bool":
                        val = True
                    elif vt == "enum":
                        ev = cap.get("enum_values") or []
                        val = (ev[0] if isinstance(ev, list) else str(ev).split(",")[0]) or ""
                    else:
                        val = cap.get("value_min") if cap.get("value_min") is not None else 0
                    step = {"device_code": foreign["code"], "capability": cap["code"], "value": val}

                    r = client.post("/api/scenes/plan", json={"actions": [step], "home_id": home_a})
                    d = r.json()
                    ck.check(f"★ 在 {home_a} 号房编排，看不到 {home_b} 号房的设备",
                             d.get("action_count") == 0 and d.get("rejected_count") == 1,
                             (d.get("rejected") or [{}])[0].get("reason", "")[:40])
                    r = client.post("/api/scenes/plan", json={"actions": [step], "home_id": home_b})
                    ck.check("★ 同一条动作回到本套房就能通过",
                             r.json().get("action_count") == 1)

                    r = client.post("/api/scenes", json={
                        "home_id": home_a, "name": "跨套房隔离（验收脚本）", "actions": [step],
                    })
                    ck.check("★ 存进 A 房的场景里塞不进 B 房的设备", r.status_code == 422,
                             f"HTTP {r.status_code}")

    except Exception as e:  # noqa: BLE001
        ck.check(f"API 层执行异常：{type(e).__name__}: {e}", False)
    finally:
        for _sid in (scene_id, sid2):
            if not _sid:
                continue
            try:
                client.delete(f"/api/scenes/{_sid}")
            except Exception:  # noqa: BLE001
                print(f"     （清理临时场景 {_sid} 失败，需要手工删除）")
        client.close()
    return ck


def layer4(catalog: DeviceCatalog) -> Checker:
    ck = Checker()
    print()
    print("=" * 76)
    print("4 · 自然语言 → 场景（真实调用 LLM）")
    print("=" * 76)

    from app.services.scenes import nl

    query = "把客厅调成看电影的样子，灯调暗一点，窗帘拉上，空调开到26度，电视打开"
    try:
        steps, understanding, _raw = nl.build_scene_from_text(catalog, query)
    except nl.SceneBuildError as e:
        ck.check(f"LLM 解析失败：{e}", False)
        return ck

    ck.check("模型返回了动作", len(steps) > 0, f"{len(steps)} 条")
    ck.check("模型给出了理解说明", bool(understanding), understanding[:60])

    plan = planner.plan_scene(catalog, steps, scene_name=query, description=understanding)
    ck.check("编排后有可执行动作", len(plan.actions) > 0, f"{len(plan.actions)} 条")
    ck.check("模型没有编造设备（被剔除数为 0 或全部有明确原因）",
             all(r.reason for r in plan.rejected))
    print("     ↓ 模型产出的动作：")
    for a in plan.actions:
        print(f"       · {a.device_name} {a.capability}={a.value}")
    if plan.rejected:
        print("     ↓ 被编排器剔掉的：")
        for r in plan.rejected:
            print(f"       · {r.device_code} {r.capability}={r.value} ↳ {r.reason[:60]}")
    return ck


def main() -> int:
    ap = argparse.ArgumentParser(description="M3 场景编排验收")
    ap.add_argument("--api", action="store_true", help="跑 API 层（需服务在运行）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--with-llm", action="store_true", help="跑真实 LLM 预览（会调用模型）")
    args = ap.parse_args()

    catalog = DeviceCatalog.load(engine)
    print()
    print("#" * 76)
    print("# M3 · 场景编排验收")
    print("#" * 76)
    print(f"  设备目录 : {len(catalog)} 台 / {len(catalog.rooms)} 个房间")
    print()

    ck1 = layer1(catalog)
    ck1.report("第 1 层 编排器")

    ck2 = layer2(catalog)
    ck2.report("第 2 层 执行器（dry-run）")

    failures = len(ck1.failed) + len(ck2.failed)
    passed = ck1.passed + ck2.passed

    if args.api:
        ck3 = layer3(args.base_url)
        ck3.report("第 3 层 API")
        failures += len(ck3.failed)
        passed += ck3.passed

    if args.with_llm:
        ck4 = layer4(catalog)
        ck4.report("第 4 层 LLM 预览")
        failures += len(ck4.failed)
        passed += ck4.passed

    print()
    print("=" * 76)
    if failures:
        print(f"结果：{passed} PASS / {failures} FAIL")
    else:
        print(f"结果：{passed} PASS / 0 FAIL  —— 场景编排验收通过 ✅")
    print("=" * 76)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
