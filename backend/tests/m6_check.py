"""M6 验收：安防告警 / 照护观察 / 主动建议。

跑法（后端必须在 8000 上）：
    python tests/m6_check.py

验收关注四类事情
----------------
1. **契约**：字段齐、过滤生效、非法参数被拒；
2. **幂等**：反复调用不会堆出重复告警/观察/建议（推导型接口最容易在这里翻车）；
3. **措辞红线**：没有"入侵已发生"、没有医疗断言、没有"已执行"；
4. **可解释性**：每条告警/建议都能说出判据和证据。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8000"
ok = 0
bad = 0
failures: list[str] = []


def call(method: str, path: str, body: dict | None = None, timeout: float = 180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def chk(name: str, cond: bool, extra: str = "") -> None:
    global ok, bad
    if cond:
        ok += 1
        print(f"  ✅ {name:<52}{extra}")
    else:
        bad += 1
        failures.append(name)
        print(f"  ❌ {name:<52}{extra}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * 78)


# ============================================================================
section("一、安防告警：契约与可解释性")
# ============================================================================
c, d = call("GET", "/api/security/alarms")
chk("GET /api/security/alarms → 200", c == 200, f"HTTP {c}")
if c != 200:
    print("   ", str(d)[:400])
    raise SystemExit(1)

#: 后面按状态过滤时要沿用同一个窗口，否则"刚确认的那条"不在默认窗口里，查不到
alarm_q = ""
if d["total"] == 0:
    # 默认窗口 = 数据锚点往前 24h。生成器重启后锚点会一下推到"此刻"，
    # 之前那批告警就落到窗外了 —— 这时不该判失败，而是换历史窗口继续校验字段契约。
    print("  ⏭️  默认窗口（最近 24h）没有告警，改用历史窗口校验字段契约")
    alarm_q = "&from=2026-10-15T00:00:00&to=2026-10-16T12:00:00"
    c, d = call("GET", "/api/security/alarms?" + alarm_q.lstrip("&"))
    chk("历史窗口能查到告警", c == 200 and d["total"] > 0, f"HTTP {c} total={d.get('total') if c == 200 else '-'}")
    if c != 200 or d["total"] == 0:
        raise SystemExit("没有可用告警数据，无法继续校验契约")
else:
    chk("  有告警数据", d["total"] > 0, f"total={d['total']}")
chk("  不再返回冗余的免责字段", "disclaimer" not in d, f"keys={sorted(d)[:6]}…")
chk("  带 sync 自证字段", isinstance(d.get("sync"), dict), str(d.get("sync")))
chk("  分级统计存在", isinstance(d.get("by_level"), dict) and bool(d["by_level"]), str(d["by_level"]))

first = d["items"][0]
for field in ("alarm_code", "level", "title", "detail", "status", "created_at", "age_minutes"):
    chk(f"  字段 {field} 存在", field in first)

detail_ok = all(
    ("判据：" in (a.get("detail") or "")) and ("证据：" in (a.get("detail") or ""))
    for a in d["items"]
)
chk("  每条告警都带判据+证据", detail_ok, f"共 {len(d['items'])} 条")
chk("  告警等级合法", all(a["level"] in ("info", "warning", "critical") for a in d["items"]))
chk("  状态合法", all(
    a["status"] in ("active", "acknowledged", "resolved", "dismissed") for a in d["items"]
))
chk("  is_active 与状态一致", all(
    a["is_active"] == (a["status"] in ("active", "acknowledged")) for a in d["items"]
))

# ============================================================================
section("二、安防告警：幂等（同一起事件不会变成多条告警）")
# ============================================================================
past_from = "2026-10-15T00:00:00"
past_to = "2026-10-16T12:00:00"
c1, d1 = call("GET", f"/api/security/alarms?from={past_from}&to={past_to}")
c2, d2 = call("GET", f"/api/security/alarms?from={past_from}&to={past_to}")
chk("固定时间窗两次调用都 200", c1 == 200 and c2 == 200)
chk("  两次 total 相同", d1["total"] == d2["total"], f"{d1['total']} vs {d2['total']}")
chk("  第二次没有新建告警", d2["sync"]["event_created"] == 0, f"created={d2['sync']['event_created']}")

# 库里不应存在"同一设备同一告警码同一秒"的重复行
from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402

with SessionLocal() as db:
    dup = db.execute(
        text(
            """
            SELECT COUNT(*) FROM (
                SELECT alarm_code, device_id, created_at, COUNT(*) n
                FROM alarms GROUP BY alarm_code, device_id, created_at HAVING n > 1
            ) t
            """
        )
    ).scalar()
chk("  库里没有重复告警行", dup == 0, f"重复组 {dup}")

# ============================================================================
section("三、告警确认：只记状态，不执行动作")
# ============================================================================
active = next((a for a in d["items"] if a["status"] == "active"), None)
if active is None:
    print("  ⏭️  （跳过确认测试：当前没有 active 告警）")
else:
    ca, da = call("POST", f"/api/security/alarms/{active['id']}/ack", {})
    chk("确认告警 → 200", ca == 200, f"HTTP {ca}")
    chk("  状态变为 acknowledged", da.get("status") == "acknowledged", str(da.get("status")))
    chk("  提示写明「确认≠已执行」", "不代表设备动作已经执行" in (da.get("message") or ""),
        (da.get("message") or "")[:40] + "…")
    chk("  提示提到待确认通道", "待确认" in (da.get("message") or ""))

    cq, dq = call("GET", f"/api/security/alarms?status=acknowledged{alarm_q}")
    chk("可按状态过滤出已确认", cq == 200 and any(a["id"] == active["id"] for a in dq["items"]),
        f"acknowledged 共 {dq.get('total')} 条")

cn, dn = call("POST", "/api/security/alarms/999999/ack", {})
chk("确认不存在的告警 → 404", cn == 404, f"HTTP {cn}")
cb, dbad = call("POST", f"/api/security/alarms/{active['id'] if active else 1}/ack",
                {"acknowledged_by": 999999})
chk("确认人不存在 → 400", cb == 400, f"HTTP {cb}")

# ============================================================================
section("四、措辞红线：不做断言")
# ============================================================================
all_text = " ".join(
    (a.get("title") or "") + (a.get("detail") or "") for a in d["items"]
)
banned_assert = ["已发生入侵", "确认入侵", "已昏迷", "已跌倒", "建议就医", "诊断为"]
hit = [w for w in banned_assert if w in all_text]
chk("告警文案没有断言式/医疗式表述", not hit, f"命中 {hit}" if hit else "0 命中")
chk("门磁类只说「被触发」不定性", "入侵" not in all_text or "被触发" in all_text,
    "措辞检查")
chk("告警详情不含「阈值为演示值」这类废话", "演示值" not in all_text,
    "0 命中" if "演示值" not in all_text else "仍有残留")

# ============================================================================
section("五、照护观察：数据覆盖与节律")
# ============================================================================
c, care = call("GET", "/api/care/observations")
chk("GET /api/care/observations → 200", c == 200, f"HTTP {c}")
chk("  有观察记录", care.get("total", 0) > 0, f"total={care.get('total')}")
chk("  返回覆盖度信息", bool(care.get("coverage")), str(care.get("coverage"))[:80])
cov = (care.get("coverage") or [{}])[0]
chk("  覆盖度含足够/不足判定", "sufficient" in cov, f"sufficient={cov.get('sufficient')}")
chk("  覆盖度含采样桶实数", "buckets_actual" in cov and "buckets_expected" in cov,
    f"{cov.get('buckets_actual')}/{cov.get('buckets_expected')}")

labels = {o["activity_label"] for o in care["items"]}
if cov.get("sufficient"):
    # 数据够：必须真的分出了"在床静息"和"活动"两类。
    # 这条是防回归的：第一版按 presence 判活动，把整夜 365 分钟记成"夜间活动"，
    # 就是因为没区分"房间有人"和"有人在动"。
    chk("  区分了「在床静息」与「活动」", "在床静息" in labels, f"类型 {sorted(labels)}")
    chk("  有起床记录", "起床" in labels, "")
else:
    # 数据不够：必须如实说"判断不了"，而不是编一个节律出来
    chk("数据不足时如实标注（不编造节律）", "数据不足，无法判断" in labels, f"类型 {sorted(labels)}")
    chk("  说明覆盖率不足的具体数字", "覆盖不足" in (care["items"][0]["remark"] or ""),
        (care["items"][0]["remark"] or "")[:52] + "…")
chk("  confidence 在 0~1", all(
    o["confidence"] is None or 0 <= o["confidence"] <= 1 for o in care["items"]
))
care_text = " ".join((o.get("remark") or "") for o in care["items"])
chk("照护文案无医疗断言", not any(w in care_text for w in banned_assert), "0 命中")
clue_items = [o for o in care["items"] if o["is_clue"]]
if clue_items:
    chk("  线索类文案写明「待确认线索」",
        all("待确认线索" in (o["remark"] or "") or "覆盖不足" in (o["remark"] or "") for o in clue_items),
        f"{len(clue_items)} 条线索")
else:
    chk("  （当前无线索类条目，跳过措辞检查）", True, "0 条线索")
chk("  不再返回冗余的免责字段", "disclaimer" not in care, f"keys={sorted(care)[:6]}…")

c2, care2 = call("GET", "/api/care/observations")
chk("  两次调用 total 相同（幂等）", care2["total"] == care["total"],
    f"{care['total']} vs {care2['total']}")

c, care_other = call("GET", "/api/care/observations?member_id=1")
chk("无感知设备的成员 → 空结果（不编造）",
    c == 200 and care_other["total"] == 0, f"total={care_other.get('total')}")

c, care_one = call("GET", "/api/care/observations?member_id=3")
chk("按成员过滤生效", c == 200 and all(o["member_id"] == 3 for o in care_one["items"]),
    f"total={care_one.get('total')}")

# ============================================================================
section("六、主动建议：分级、理由、免打扰")
# ============================================================================
c, sug = call("GET", "/api/suggestions")
chk("GET /api/suggestions → 200", c == 200, f"HTTP {c}")
chk("  有建议", sug.get("total", 0) > 0, f"total={sug.get('total')}")
chk("  每条都带 reason", all(isinstance(s.get("reason"), dict) and s["reason"] for s in sug["items"]))
chk("  reason 里有 rule 名", all("rule" in s["reason"] for s in sug["items"]),
    str({s["reason"].get("rule") for s in sug["items"]}))
chk("  等级合法", all(
    s["disturbance_level"] in ("urgent", "important", "normal", "silent") for s in sug["items"]
))
order = {lv: i for i, lv in enumerate(sug["order"])}
idx = [order[s["disturbance_level"]] for s in sug["items"]]
chk("  按打扰等级从高到低排序", idx == sorted(idx), f"顺序 {sug['order']}")
chk("  quiet_downgraded 与 reason 一致", all(
    s["quiet_downgraded"] == bool(s["reason"].get("downgraded_from")) for s in sug["items"]
))

sug_text = " ".join(s["content"] for s in sug["items"])
chk("建议文案不含「已执行/已关闭」", not any(
    w in sug_text for w in ("已关闭", "已执行", "已上锁", "已断电")
), "0 命中")

# 免打扰降级：至少能查出 normal/silent 的统计
c, sug_quiet = call("GET", "/api/suggestions?level=silent")
chk("可按等级过滤（silent）", c == 200, f"total={sug_quiet.get('total')}")

# 忽略 / 采纳
pending = next((s for s in sug["items"] if s["status"] == "pending"), None)
if pending is None:
    print("  ⏭️  （跳过忽略/采纳测试：当前没有 pending 建议）")
else:
    cd_, dd_ = call("POST", f"/api/suggestions/{pending['id']}/dismiss", {})
    chk("忽略建议 → 200", cd_ == 200 and dd_.get("status") == "dismissed", f"HTTP {cd_}")
    chk("  提示说明 12 小时内不再重复", "12 小时" in (dd_.get("message") or ""),
        (dd_.get("message") or "")[:34] + "…")

pending2 = next((s for s in sug["items"] if s["status"] == "pending" and s["id"] != (pending and pending["id"])), None)
if pending2 is not None:
    ca_, da_ = call("POST", f"/api/suggestions/{pending2['id']}/accept", {})
    chk("采纳建议 → 200", ca_ == 200 and da_.get("status") == "accepted", f"HTTP {ca_}")
    chk("  明确「没有执行任何设备动作」",
        "没有执行任何设备动作" in (da_.get("message") or ""),
        (da_.get("message") or "")[:36] + "…")

cn, _ = call("POST", "/api/suggestions/999999/dismiss", {})
chk("忽略不存在的建议 → 404", cn == 404, f"HTTP {cn}")

# ============================================================================
section("七、参数校验与兼容")
# ============================================================================
c, _ = call("GET", "/api/security/alarms?from=2026-10-16T12:00:00&to=2026-10-15T00:00:00")
chk("from 晚于 to → 400", c == 400, f"HTTP {c}")
c, _ = call("GET", "/api/security/alarms?from=2026-01-01T00:00:00&to=2026-10-16T00:00:00")
chk("时间跨度过大 → 400", c == 400, f"HTTP {c}")
c, d = call("GET", "/api/security/alarms?sync=false")
chk("sync=false 可关闭推导", c == 200 and d["sync"]["event_created"] == 0,
    f"created={d['sync']['event_created']}")
c, d = call("GET", "/api/care/observations?sync=false")
chk("照护 sync=false 可用", c == 200, f"HTTP {c}")
c, d = call("GET", "/api/security/alarms?level=critical")
chk("按等级过滤生效（critical）", c == 200 and all(a["level"] == "critical" for a in d["items"]),
    f"total={d.get('total')}")

# ============================================================================
print("\n" + "=" * 78)
print(f"  结果：{ok} PASS / {bad} FAIL")
if failures:
    print("  失败项：")
    for f in failures:
        print(f"    · {f}")
print("=" * 78)
sys.exit(0 if bad == 0 else 1)
