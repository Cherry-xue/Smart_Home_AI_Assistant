"""M5 Web 大屏 · 契约对账。

前端（`frontend/dashboard/`）把后端字段写死在 `src/api/` 和各页面里。
后端一旦改了 schema，前端不会报错、只会**显示成空白或 —— **，这种问题最难查。

所以这个脚本做两件事：
1. 前端用到的每个端点，在 OpenAPI 里必须存在（路径 + 方法）；
2. 前端渲染依赖的每个字段，在真实响应里必须存在（字段名 + 大致类型）。

它不替代浏览器 —— 只保证"数据这一侧不会悄悄变"。

用法：
    python tests/web_check.py                 # 默认 http://127.0.0.1:8000
    python tests/web_check.py --base-url ...
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# 与其他验收脚本一致：测试自包含，Check 工具就地定义（不引公共模块）
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

    def report(self) -> None:
        print()
        print("-" * 76)
        if self.failed:
            print(f"{self.title}: {self.passed} PASS / {len(self.failed)} FAIL")
            for f in self.failed:
                print(f"    ❌ {f}")
        else:
            print(f"{self.title}: {self.passed} PASS / 0 FAIL")
        print("-" * 76)


def summary(ck: Checker) -> int:
    ck.report()
    print()
    print("=" * 76)
    if ck.failed:
        print(f"结果：{ck.passed} PASS / {len(ck.failed)} FAIL")
    else:
        print(f"结果：{ck.passed} PASS / 0 FAIL  —— Web 契约对账通过 ✅")
    print("=" * 76)
    return 1 if ck.failed else 0


# ⚠️ 本机可能设了 HTTP_PROXY，会把 127.0.0.1 也走代理 → 全部 502。
# 这里显式禁掉代理，别被这个假象误导。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, timeout: float = 30.0) -> Any:
    with _opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, payload: dict | None = None, timeout: float = 60.0) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# 前端 src/api/index.js 里出现的全部端点
EXPECTED_ENDPOINTS = [
    ("get", "/api/health"),
    ("get", "/api/homes/{home_id}/rooms"),
    ("get", "/api/devices"),
    ("get", "/api/devices/{device_id}"),
    ("get", "/api/devices/{device_id}/readings"),
    ("get", "/api/devices/{device_id}/capabilities"),
    ("get", "/api/energy/summary"),
    ("post", "/api/chat"),
    ("post", "/api/chat/stream"),
    ("get", "/api/confirmations/pending"),
    ("post", "/api/confirmations/{thread_id}"),
    ("post", "/api/scenes/preview"),
    ("post", "/api/scenes/plan"),
    ("get", "/api/scenes"),
    ("post", "/api/scenes"),
    ("get", "/api/scenes/{scene_id}"),
    ("post", "/api/scenes/{scene_id}/run"),
    ("get", "/api/knowledge/search"),
    ("get", "/api/knowledge/documents"),
    ("get", "/api/knowledge/status"),
]


def check_openapi(ck: Checker, base: str) -> None:
    print("\n[1] 前端用到的端点必须在 OpenAPI 里存在")
    try:
        spec = _get(f"{base}/openapi.json")
    except Exception as e:  # noqa: BLE001
        ck.check("能取到 OpenAPI 文档", False, f"{type(e).__name__}: {e}")
        return

    paths = spec.get("paths") or {}
    for method, path in EXPECTED_ENDPOINTS:
        ck.check(f"{method.upper()} {path}", method in (paths.get(path) or {}))


def check_health(ck: Checker, base: str) -> None:
    print("\n[2] /api/health —— 顶栏与系统页依赖的字段")
    d = _get(f"{base}/api/health")

    for k in ("status", "app_env", "data_mode", "timezone", "generated_at", "data_anchor", "tariff"):
        ck.check(f"health.{k} 存在", k in d)

    # ⚠️ 这里曾经踩过坑：键名是 total 不是 devices
    dev = d.get("devices") or {}
    for k in ("total", "rooms", "high_risk", "online"):
        ck.check(f"health.devices.{k} 存在", k in dev, f"实际键：{list(dev)}")

    for k in ("enabled", "connected", "messages_seen", "last_message_at"):
        ck.check(f"health.mqtt.{k} 存在", k in (d.get("mqtt") or {}))

    for k in ("received", "readings", "energies", "events", "rejected", "unknown_device", "stale"):
        ck.check(f"health.ingest.{k} 存在", k in (d.get("ingest") or {}))

    for k in ("devices", "values", "preloaded", "offline_count"):
        ck.check(f"health.cache.{k} 存在", k in (d.get("cache") or {}))

    # 红线字段：前端侧栏常驻展示，必须恒为 false
    ck.check(
        "🚨 high_risk_auto_exec 恒为 false",
        d.get("high_risk_auto_exec") is False,
        f"实际：{d.get('high_risk_auto_exec')}",
    )


def check_rooms(ck: Checker, base: str) -> None:
    print("\n[3] /api/homes/1/rooms —— 房间卡片墙")
    d = _get(f"{base}/api/homes/1/rooms")
    items = d.get("items") or []
    ck.check("返回房间", len(items) > 0, f"{len(items)} 间")

    need = (
        "room_id", "code", "name", "device_count", "online_count", "offline_count",
        "high_risk_count", "active_alarm_count", "recent_event_count",
        "last_update_at", "key_metrics",
    )
    if items:
        first = items[0]
        for k in need:
            ck.check(f"room.{k} 存在", k in first, f"缺 {k}" if k not in first else "")

        km = (first.get("key_metrics") or [])
        if km:
            for k in ("metric_code", "value", "unit", "device_id", "device_name", "observed_at", "quality"):
                ck.check(f"key_metric.{k} 存在", k in km[0])
        else:
            ck.check("key_metrics 非空", False, "第一间房没有关键指标")


def check_devices(ck: Checker, base: str) -> None:
    print("\n[4] /api/devices —— 设备列表页")
    d = _get(f"{base}/api/devices")
    items = d.get("items") or []
    ck.check("返回设备", len(items) > 0, f"{len(items)} 台")
    ck.check("meta.summary 存在", bool((d.get("meta") or {}).get("summary")))

    need = (
        "id", "code", "name", "device_type", "room_code", "room_name",
        "high_risk", "auto_exec_allowed", "online", "last_observed_at",
        "capability_count", "writable_count", "latest",
    )
    if items:
        first = items[0]
        for k in need:
            ck.check(f"device.{k} 存在", k in first, f"缺 {k}" if k not in first else "")

    # 高风险设备必须 auto_exec_allowed=false（前端要据此画锁）
    hr = [x for x in items if x.get("high_risk")]
    ck.check("存在高风险设备", len(hr) > 0, f"{len(hr)} 台")
    ck.check(
        "🚨 高风险设备 auto_exec_allowed 全为 false",
        all(x.get("auto_exec_allowed") is False for x in hr),
    )

    # 详情三件套
    if items:
        did = items[0]["id"]
        caps = _get(f"{base}/api/devices/{did}/capabilities")
        for k in ("device_id", "code", "name", "capabilities"):
            ck.check(f"capabilities.{k} 存在", k in caps)
        if caps.get("capabilities"):
            for k in ("code", "display_name", "value_type", "unit", "value_min", "value_max", "enum_values", "writable", "risk_level"):
                ck.check(f"capability.{k} 存在", k in caps["capabilities"][0])

        rd = _get(f"{base}/api/devices/{did}/readings")
        for k in ("device_id", "row_count", "stale_rows", "quality_summary", "metrics"):
            ck.check(f"readings.{k} 存在", k in rd)
        metrics = rd.get("metrics") or {}
        if metrics:
            m = next(iter(metrics.values()))
            for k in ("metric_code", "unit", "count", "points"):
                ck.check(f"metric.{k} 存在", k in m)
            if m.get("points"):
                for k in ("observed_at", "value", "quality", "is_stale"):
                    ck.check(f"point.{k} 存在", k in m["points"][0])


def check_energy(ck: Checker, base: str) -> None:
    print("\n[5] /api/energy/summary —— 能耗页")
    d = _get(f"{base}/api/energy/summary")
    for k in ("from", "to", "currency", "unit", "total_kwh", "total_cost",
              "by_period", "by_device", "hourly", "observations"):
        ck.check(f"energy.{k} 存在", k in d, f"缺 {k}" if k not in d else "")

    if d.get("by_period"):
        for k in ("period", "price", "kwh", "cost", "share", "avg_power_w", "peak_power_w"):
            ck.check(f"period.{k} 存在", k in d["by_period"][0])
        # 前端把 share 当 0~1 的比例 *100 显示
        ck.check(
            "share 是 0~1 的比例（前端按此 *100）",
            all(0 <= (p.get("share") or 0) <= 1 for p in d["by_period"]),
            f"实际：{[p.get('share') for p in d['by_period']]}",
        )

    if d.get("by_device"):
        for k in ("device_id", "name", "room_name", "kwh", "cost", "share", "avg_power_w", "peak_power_w"):
            ck.check(f"device_usage.{k} 存在", k in d["by_device"][0])

    if d.get("hourly"):
        for k in ("hour", "kwh", "avg_power_w"):
            ck.check(f"hourly.{k} 存在", k in d["hourly"][0])


def check_scenes(ck: Checker, base: str) -> None:
    print("\n[6] /api/scenes —— 场景编排页")
    try:
        pv = _post(f"{base}/api/scenes/preview", {"query": "把客厅调成看电影的样子"})
    except urllib.error.HTTPError as e:
        # 模型额度/服务不可用时这个接口会 5xx —— 那是环境问题，不算契约错
        ck.check("scenes/preview 可调用", False, f"HTTP {e.code}（可能是模型服务不可用）")
        return

    for k in ("query", "understanding", "intent_key", "intent_label", "raw_count",
              "action_count", "rejected_count", "executable_count",
              "pending_confirm_count", "actions", "rejected", "notes"):
        ck.check(f"preview.{k} 存在", k in pv, f"缺 {k}" if k not in pv else "")

    if pv.get("actions"):
        for k in ("device_code", "device_name", "room_name", "capability", "value",
                  "status", "reason", "risk_level", "high_risk_device"):
            ck.check(f"action.{k} 存在", k in pv["actions"][0])

    ck.check("场景列表可取", isinstance(_get(f"{base}/api/scenes"), list))


def check_knowledge(ck: Checker, base: str) -> None:
    print("\n[7] /api/knowledge —— 知识库页")
    st = _get(f"{base}/api/knowledge/status")
    for k in ("collection", "chunks", "embedding_model", "chroma_dir", "raw_files", "indexed_documents"):
        ck.check(f"kb_status.{k} 存在", k in st)

    docs = _get(f"{base}/api/knowledge/documents")
    ck.check("文档清单非空", len(docs) > 0, f"{len(docs)} 份")
    if docs:
        for k in ("source_file", "doc_title", "doc_type", "sections", "chunks"):
            ck.check(f"doc.{k} 存在", k in docs[0])

    try:
        hits = _get(f"{base}/api/knowledge/search?q=" + urllib.parse.quote("空调滤网多久清洗一次"))
        for k in ("query", "k", "count", "elapsed_ms", "collection_size", "hits", "note"):
            ck.check(f"search.{k} 存在", k in hits)
        if hits.get("hits"):
            for k in ("content", "distance", "source_file", "doc_title", "doc_type", "section", "device_types"):
                ck.check(f"hit.{k} 存在", k in hits["hits"][0])
    except urllib.error.HTTPError as e:
        ck.check("knowledge/search 可调用", False, f"HTTP {e.code}（可能是向量模型不可用）")


def check_chat(ck: Checker, base: str) -> None:
    print("\n[8] /api/chat —— 对话页（非流式）")
    try:
        out = _post(f"{base}/api/chat", {"question": "现在家里耗电情况怎么样", "home_id": 1})
    except urllib.error.HTTPError as e:
        ck.check("chat 可调用", False, f"HTTP {e.code}")
        return

    for k in ("thread_id", "interrupted", "answer", "sections", "trace",
              "intervention", "route", "plan_rejected", "run_result"):
        ck.check(f"chat.{k} 存在", k in out, f"缺 {k}" if k not in out else "")

    # 待确认列表：手机端与大屏侧栏都轮询它
    pend = _get(f"{base}/api/confirmations/pending")
    for k in ("items", "total"):
        ck.check(f"pending.{k} 存在", k in pend)
    if pend.get("items"):
        for k in ("thread_id", "kind", "device_name", "capability", "prompt", "options", "expires_at"):
            ck.check(f"intervention.{k} 存在", k in pend["items"][0])


def main() -> int:
    ap = argparse.ArgumentParser(description="Web 大屏契约对账")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    ck = Checker("M5 Web 大屏 · 契约对账")
    print(f"后端：{base}")
    print("提示：这个脚本只查数据契约，不替代浏览器里的视觉检查。")

    try:
        check_openapi(ck, base)
        check_health(ck, base)
        check_rooms(ck, base)
        check_devices(ck, base)
        check_energy(ck, base)
        check_scenes(ck, base)
        check_knowledge(ck, base)
        check_chat(ck, base)
    except Exception as e:  # noqa: BLE001
        ck.check("脚本执行完成", False, f"{type(e).__name__}: {e}")

    return summary(ck)


if __name__ == "__main__":
    raise SystemExit(main())
