"""M4 多 Agent 编排验收。

跑法：
    cd backend
    python tests/agent_check.py              # 第 1~4 层（离线，不联网不调模型）
    python tests/agent_check.py --api        # 加第 5 层（需 API 在运行）
    python tests/agent_check.py --with-llm   # 加第 6 层真实模型（会消耗额度）

六层
----
1. **工具层白名单** —— 每个 Agent 能碰到什么；断言"工具层不存在执行通道"。
2. **图拓扑** —— 节点清单、扇出条件边、缓存。
3. **确定性主干（打桩 Agent，不联网）** —— 汇总 → 编排 → 低风险执行 → 中断 →
   恢复 → 高风险执行。**这是 M4 的核心验收**。
4. **路由降级** —— 模型不可用时不让整个请求失败。
5. **API** —— `/api/chat`、`/api/chat/stream`、`/api/confirmations/*`。
6. **真实模型** —— 路由分类与专业 Agent 真的能产出结构化结论（可选）。

为什么要"打桩"第 3 层
----------------------
M4 的分工是：**模型负责"想做什么"，代码负责"能不能做、做了会怎样"**。
第 3 层验的是后半句 —— 它必须**完全确定性、可复现**，
不能因为模型额度、网络抖动、模型改版而今天过明天不过。

所以这里把 5 个专业 Agent 换成"返回预置结论"的桩节点，主干代码（汇总/编排/
执行/确认）仍然是**生产代码本身**，没有替换。要验模型那一段请跑第 6 层。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

#: 每次运行用不同的会话标识 —— 否则"幂等复用待确认指令"会命中上一轮跑测试留下的
#: 旧数据，让"新增了几条指令"这类断言失真（生产环境里 thread_id 本来就是唯一的）
RUN_TAG = uuid.uuid4().hex[:6]

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Windows 控制台默认 GBK，中文会炸；统一切成 utf-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from app.core.asyncio_compat import ensure_selector_loop_on_windows  # noqa: E402

ensure_selector_loop_on_windows()

from sqlalchemy import text  # noqa: E402

from app.agent import ALL_AGENTS, AGENT_LABELS  # noqa: E402
from app.agent.models import Decision, Fact, Finding, ProposedAction  # noqa: E402
from app.db.catalog import DeviceCatalog  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.tools import ALL_TOOL_NAMES, ToolContext, build_tools  # noqa: E402

# ============================================================================
# 小工具
# ============================================================================


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


def broker_alive() -> bool:
    """broker 是否在跑。**不影响红线判定**，只影响"低风险动作最终记为 executed 还是 failed"。"""
    from app.config.settings import get_settings

    s = get_settings()
    try:
        with socket.create_connection((s.mqtt_host, s.mqtt_port), timeout=1.0):
            return True
    except OSError:
        return False


def max_command_id() -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COALESCE(MAX(id),0) FROM device_commands")).scalar() or 0)


def commands_since(since_id: int) -> list[dict[str, Any]]:
    """取某条基线之后新增的指令记录（带设备编码，便于断言）。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.id, d.code, c.command, c.status, c.risk_level, c.issued_by, c.result_message
                FROM device_commands c
                JOIN devices d ON d.id = c.device_id
                WHERE c.id > :since
                ORDER BY c.id
                """
            ),
            {"since": since_id},
        ).all()
    return [
        {
            "id": r.id,
            "device": r.code,
            "command": r.command,
            "status": r.status,
            "risk_level": r.risk_level,
            "issued_by": r.issued_by,
            "result_message": r.result_message,
        }
        for r in rows
    ]


# ============================================================================
# 第 1 层：工具层白名单
# ============================================================================


def layer1(ctx: ToolContext) -> Checker:
    ck = Checker()
    print("=" * 76)
    print("1 · 工具层（白名单 / 权限表）")
    print("=" * 76)

    from app.agent.specialists import AGENT_TOOLS

    # ---- 1.1 白名单可用 ----
    for agent in ALL_AGENTS:
        names = AGENT_TOOLS.get(agent, ())
        try:
            tools = build_tools(ctx, names)
            ok = len(tools) == len(set(names))
            ck.check(f"{AGENT_LABELS[agent]}（{agent}）工具可构建", ok,
                     f"{len(tools)} 个：{', '.join(t.name for t in tools)}")
        except Exception as e:  # noqa: BLE001
            ck.check(f"{AGENT_LABELS[agent]}（{agent}）工具可构建", False, f"{type(e).__name__}: {e}")

    # ---- 1.2 表里的名字都在登记范围内 ----
    declared = set(ALL_TOOL_NAMES)
    stray = {n for names in AGENT_TOOLS.values() for n in names} - declared
    ck.check("白名单里没有未登记的工具名", not stray, f"越界：{sorted(stray)}" if stray else "")

    # ---- 1.3 未知工具名必须立刻报错（拼错不能静默少给）----
    try:
        build_tools(ctx, ["no_such_tool"])
        ck.check("未知工具名报错", False, "竟然静默通过了")
    except ValueError as e:
        ck.check("未知工具名报错", "no_such_tool" in str(e))

    # ---- 1.4 红线：工具层不存在执行通道 ----
    all_built = build_tools(ctx, sorted(ALL_TOOL_NAMES))
    exec_like = [
        t.name for t in all_built
        if any(k in t.name.lower() for k in ("execute", "unlock", "open_valve", "shutoff", "arm_", "disarm"))
    ]
    ck.check("工具层不存在高风险执行通道", not exec_like, f"发现：{exec_like}" if exec_like else f"共 {len(all_built)} 个工具，均为只读/检索类")

    # ---- 1.5 每个 Agent 的工具集互不相同（权限而非配置）----
    sigs = {agent: frozenset(names) for agent, names in AGENT_TOOLS.items()}
    ck.check("5 个 Agent 的工具集各不相同（按职责分权）", len(set(sigs.values())) == len(sigs))

    return ck


# ============================================================================
# 第 2 层：图拓扑
# ============================================================================


def layer2(ctx: ToolContext) -> Checker:
    ck = Checker()
    print()
    print("=" * 76)
    print("2 · 图拓扑（节点 / 扇出 / 缓存）")
    print("=" * 76)

    import app.agent.graph as G

    saved_specialist, saved_route = G.build_specialist_node, G.route_node
    G.build_specialist_node = _stub_specialist({})
    G.route_node = _stub_route(["scene"])
    try:
        graph = G.build_graph(ctx)
    finally:
        G.build_specialist_node, G.route_node = saved_specialist, saved_route

    info = G.graph_info()
    nodes = set(info["nodes"])
    expected = {"route", "aggregate", "plan", "execute_low", "confirm", "execute_high", "respond"}
    expected |= {f"{a}_node" for a in ALL_AGENTS}
    ck.check("节点齐全（5 个专业 Agent + 6 个主干节点）", nodes == expected,
             f"{len(nodes)} 个节点")
    ck.check("专业 Agent 节点与 ALL_AGENTS 一致", all(f"{a}_node" in nodes for a in ALL_AGENTS))

    # 扇出：有助手 → Send 到对应节点；无助手 → 直接 aggregate
    from app.agent.models import RouteDecision
    from app.agent.state import initial_state

    s1 = initial_state("把客厅灯调暗")
    s1["route"] = RouteDecision(intents=["控制"], agents=["scene", "energy"], reason="t")
    fan = G._fan_out(s1)
    ck.check("有助手时按结论扇出", isinstance(fan, list) and len(fan) == 2,
             f"{[getattr(x, 'node', x) for x in fan] if isinstance(fan, list) else fan}")

    s2 = initial_state("空调说明书怎么说的")
    s2["route"] = RouteDecision(intents=["问答"], agents=[], reason="t")
    ck.check("纯问答不激活任何助手 → 直接汇总", G._fan_out(s2) == "aggregate")

    s3 = initial_state("随便问问")
    s3["route"] = RouteDecision(intents=["闲聊"], agents=["scene", "not_an_agent"], reason="t")
    fan3 = G._fan_out(s3)
    ck.check("路由给出未登记的 Agent 名会被过滤", isinstance(fan3, list) and len(fan3) == 1)

    # 缓存：同一个 ctx 只编译一次
    G.reset_graph_cache()
    G.get_graph(ctx)
    G.get_graph(ctx)
    ck.check("图按 ctx 缓存（不重复编译）", G.graph_info()["cached_graphs"] == 1)

    # 图能真的画出来（编译产物自检）
    ck.check("图编译成功（可 get_graph / ainvoke）", graph is not None)

    return ck


# ============================================================================
# 第 3 层：确定性主干端到端（打桩 Agent，不联网）
# ============================================================================

#: 招牌用例：看电影 + 顺手锁门。
#: - 灯带 80% 是**故意混入的矛盾动作**，应被观影意图剔除（验证意图约束在 Agent 链路里同样生效）
#: - 门锁是**高风险**，应触发中断，且任何阶段都不会变成 executed
MOVIE_FINDING = Finding(
    agent="scene",
    summary="把客厅调成观影氛围，并顺手把入户门锁上",
    facts=[
        Fact(label="客厅温度", value=27.5, unit="℃", device_code="living_room_temp_sensor",
             observed_at=dt.datetime.now().isoformat(timespec="seconds"), quality="good"),
    ],
    analyses=["当前 27.5℃，观影久坐建议 24~27℃"],
    proposed_actions=[
        ProposedAction(device_code="living_room_light_main", capability="brightness",
                       value=30, reason="观影需要环境偏暗"),
        ProposedAction(device_code="living_room_curtain", capability="position",
                       value=0, reason="观影需要遮光"),
        ProposedAction(device_code="living_room_tv", capability="power_on",
                       value=True, reason="打开电视"),
        ProposedAction(device_code="living_room_light_strip", capability="brightness",
                       value=80, reason="（故意混入）比主灯还亮，与观影氛围矛盾"),
        ProposedAction(device_code="front_door_lock", capability="lock_state",
                       value="locked", reason="顺手锁门"),
    ],
    uncertainties=["模拟数据，非真实实测值"],
)

#: 纯低风险用例：不中断，直接执行完
LIGHT_ONLY_FINDING = Finding(
    agent="scene",
    summary="把客厅主灯调到 40%",
    proposed_actions=[
        ProposedAction(device_code="living_room_light_main", capability="brightness",
                       value=40, reason="用户要求调暗"),
    ],
)


def _stub_specialist(findings: dict[str, Finding]):
    """把专业 Agent 换成"返回预置结论"的桩。主干代码仍是生产代码。"""
    from app.agent.state import TraceEntry

    def builder(agent: str, ctx: Any):
        async def node(state: dict) -> dict:
            f = findings.get(agent)
            if f is None:
                return {"trace": [TraceEntry(node=agent, label=AGENT_LABELS.get(agent, agent),
                                             detail="（桩）未激活", elapsed_ms=0)]}
            return {
                "findings": [f],
                "trace": [TraceEntry(node=agent, label=AGENT_LABELS.get(agent, agent),
                                     detail=f"（桩）{len(f.proposed_actions)} 个动作建议",
                                     elapsed_ms=0)],
            }

        node.__name__ = f"{agent}_node"
        return node

    return builder


def _stub_route(agents: list[str], rooms: list[str] | None = None):
    from app.agent.models import RouteDecision
    from app.agent.state import TraceEntry

    async def node(state: dict) -> dict:
        return {
            "route": RouteDecision(intents=["测试"], agents=list(agents),
                                   rooms=list(rooms or []), reason="（桩）固定路由"),
            "trace": [TraceEntry(node="router", label="理解你的意图",
                                 detail=f"（桩）{agents}", elapsed_ms=0)],
        }

    return node


def _build_stub_graph(ctx: ToolContext, findings: dict[str, Finding], agents: list[str]):
    import app.agent.graph as G

    saved = (G.build_specialist_node, G.route_node)
    G.build_specialist_node = _stub_specialist(findings)
    G.route_node = _stub_route(agents)
    try:
        return G.build_graph(ctx)
    finally:
        G.build_specialist_node, G.route_node = saved


def layer3(ctx: ToolContext) -> Checker:
    ck = Checker()
    alive = broker_alive()
    print()
    print("=" * 76)
    print("3 · 确定性主干（打桩 Agent · 端到端）")
    print("=" * 76)
    print(f"  注：MQTT broker {'已运行（低风险动作会记 executed）' if alive else '未运行（低风险动作会记 failed，属预期降级）'}")
    print()

    try:
        asyncio.run(_layer3_async(ctx, ck, alive))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        ck.check("端到端跑通", False, f"{type(e).__name__}: {e}")
    return ck


async def _layer3_async(ctx: ToolContext, ck: Checker, alive: bool) -> None:
    from app.agent import runner

    # ======================================================================
    # 3.1 纯低风险 —— 不该中断，直接执行完
    # ======================================================================
    print("3.1 纯低风险：不下中断，直接执行")
    g1 = _build_stub_graph(ctx, {"scene": LIGHT_ONLY_FINDING}, ["scene"])
    base = max_command_id()
    t1 = f"chk-low-{RUN_TAG}"
    out1 = await runner.run("把客厅主灯调到 40%", thread_id=t1, ctx=ctx, graph=g1, home_id=1)

    ck.check("纯低风险不中断", out1["interrupted"] is False)
    ck.check("给出了回答", bool(out1["answer"]), out1["answer"][:40])
    rr1 = out1.get("run_result") or {}
    ck.check("低风险动作已下发", len(rr1.get("executed", [])) + len(rr1.get("failed", [])) >= 1,
             f"executed={len(rr1.get('executed', []))} failed={len(rr1.get('failed', []))}")
    rows1 = commands_since(base)
    ck.check("指令记录已落库（审计留痕）", len(rows1) >= 1,
             "; ".join(f"{r['device']}.{r['command']}={r['status']}" for r in rows1[:4]))
    ck.check("低风险指令没有被写成待确认",
             all(r["status"] != "pending_confirm" for r in rows1))
    print()

    # ======================================================================
    # 3.2 含高风险 —— 应该中断，且低风险已经先执行了
    # ======================================================================
    print("3.2 含高风险：低风险先执行 → 高风险停住等人")
    g2 = _build_stub_graph(ctx, {"scene": MOVIE_FINDING}, ["scene"])
    base = max_command_id()
    t2 = f"chk-high-{RUN_TAG}"
    out2 = await runner.run("我要看电影了，顺便把门锁上", thread_id=t2, ctx=ctx, graph=g2, home_id=1)

    ck.check("高风险触发中断", out2["interrupted"] is True)
    ck.check("中断载荷结构正确",
             isinstance(out2["intervention"], dict)
             and (out2["intervention"].get("interventions") or []),
             f"{len((out2['intervention'] or {}).get('interventions', []))} 条待确认")

    items = (out2["intervention"] or {}).get("interventions") or []
    ck.check("待确认项指向高风险设备",
             any(i.get("device_code") == "front_door_lock" for i in items),
             ", ".join(i.get("device_name", "") for i in items))
    ck.check("待确认项给出了选项与有效期",
             all(i.get("options") and i.get("expires_at") for i in items))
    ck.check("待确认项绑定了指令主键（可反查 / 可从库里重建列表）",
             all(i.get("command_id") for i in items),
             f"command_id={[i.get('command_id') for i in items]}")

    # 意图约束：与观影氛围矛盾的灯带 80% 应被剔除并写明原因
    rejected = out2.get("plan_rejected") or []
    strip = [r for r in rejected if r.get("device_code") == "living_room_light_strip"]
    ck.check("矛盾动作被意图规则剔除（且写明原因）",
             bool(strip) and "观影" in (strip[0].get("reason") or ""),
             strip[0].get("reason", "") if strip else f"被剔除：{[r.get('device_code') for r in rejected]}")

    # ⭐ 关键：中断**之前**低风险动作就已经执行了
    rr2 = out2.get("run_result") or {}
    low_done = len(rr2.get("executed", [])) + len(rr2.get("failed", []))
    ck.check("低风险动作在中断前已执行（不用等人确认）", low_done >= 3,
             f"executed={len(rr2.get('executed', []))} failed={len(rr2.get('failed', []))}")

    rows2 = commands_since(base)
    lock_rows = [r for r in rows2 if r["device"] == "front_door_lock"]
    low_rows = [r for r in rows2 if r["device"] != "front_door_lock"]
    ck.check("🚨 高风险指令恒为 pending_confirm（红线）",
             bool(lock_rows) and all(r["status"] == "pending_confirm" for r in lock_rows),
             "; ".join(f"{r['device']}={r['status']}" for r in lock_rows))
    ck.check("低风险指令没有因为等待确认而被挂起",
             bool(low_rows) and all(r["status"] != "pending_confirm" for r in low_rows),
             "; ".join(f"{r['device']}={r['status']}" for r in low_rows))

    # 待确认索引（手机端轮询用）
    from app.agent.runner import get_pending, list_pending_threads
    ck.check("待确认索引已登记（供手机端列表查询）",
             get_pending(t2) is not None and len(list_pending_threads()) >= 1)

    sections = out2.get("answer_sections") or {}
    ck.check("回答分区完整（观测事实 / 分析 / 待确认）",
             "observed_facts" in sections and "analyses" in sections,
             f"事实 {len(sections.get('observed_facts', []))} 条"
             f"／分析 {len(sections.get('analyses', []))} 条")
    print()

    # ======================================================================
    # 3.3 恢复 —— 批准
    # ======================================================================
    print("3.3 恢复：人工批准")
    base = max_command_id()
    out3 = await runner.resume(t2, [Decision(intervention_index=0, choice="approve")], ctx=ctx, graph=g2)

    ck.check("恢复后不再中断（走完 respond）", out3["interrupted"] is False)
    rr3 = out3.get("run_result") or {}
    # ⚠️ 只看**高风险那台**：run_result 是低风险与高风险两段合并的结果，
    # broker 在线时 executed 里会有 3 个低风险动作，那是预期行为不是红线失守。
    hr_executed = [s for s in rr3.get("executed", []) if s.get("device_code") == "front_door_lock"]
    hr_pending = [s for s in rr3.get("pending_confirm", []) if s.get("device_code") == "front_door_lock"]
    # 红线口径已从"永不执行"精确成"**绝不自动执行，人工授权后才执行**"：
    # 批准是授权通道，所以这里应该真的下发（executed）；没批准前必须一只都不动。
    ck.check("批准后高风险动作真的被下发（人工授权通道生效）",
             len(hr_executed) >= 1 and not hr_pending,
             f"门锁 executed={len(hr_executed)} pending={len(hr_pending)}"
             f"｜全部 executed={len(rr3.get('executed', []))}（含低风险）")
    ck.check("恢复后给出了最终回答", bool(out3["answer"]), out3["answer"][:50])

    rows3 = commands_since(base)
    ck.check("待确认索引已清除", get_pending(t2) is None)

    # 批准只是把已存在的那一行往下推（pending_confirm → confirmed → executed），
    # 不该再插一条（否则手机端会看到重复待办）
    ck.check("批准是更新既有待确认项，而不是新增一条",
             len(rows3) == 0, f"新增 {len(rows3)} 条：{[r['device'] for r in rows3]}")

    # 全库扫描红线的**现在形态**：高风险指令可以被执行，但每一条都必须有人工确认时间。
    # 没有 confirmed_at 的 executed 就是"绕过人工确认自动执行了"——那才是真违规。
    with engine.connect() as conn:
        bad = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM device_commands c
                JOIN devices d ON d.id = c.device_id
                WHERE d.high_risk = 1
                  AND c.status IN ('executed', 'confirmed')
                  AND c.confirmed_at IS NULL
                """
            )
        ).scalar()
    ck.check("🚨 全库校验：高风险指令凡执行必有人工确认时间", int(bad or 0) == 0,
             f"无 confirmed_at 的已执行指令 {bad} 条")
    print()

    # ======================================================================
    # 3.4 恢复 —— 驳回
    # ======================================================================
    print("3.4 恢复：人工驳回")
    g4 = _build_stub_graph(ctx, {"scene": MOVIE_FINDING}, ["scene"])
    base = max_command_id()
    t4 = f"chk-rej-{RUN_TAG}"
    await runner.run("我要看电影了，顺便把门锁上", thread_id=t4, ctx=ctx, graph=g4, home_id=1)
    before = len([r for r in commands_since(base) if r["device"] == "front_door_lock"])

    out4 = await runner.resume(t4, [Decision(intervention_index=0, choice="reject")], ctx=ctx, graph=g4)
    rr4 = out4.get("run_result") or {}
    hr_executed4 = [s for s in rr4.get("executed", []) if s.get("device_code") == "front_door_lock"]
    ck.check("驳回后高风险动作不执行", not hr_executed4, f"门锁 executed={len(hr_executed4)}")
    after = len([r for r in commands_since(base) if r["device"] == "front_door_lock"])
    ck.check("驳回没有新增待确认指令（不重复骚扰用户）", after == before, f"{before} → {after}")
    print()

    # ======================================================================
    # 3.5 多 Agent 并行扇出
    # ======================================================================
    print("3.5 多 Agent 并行：结论合并")
    energy_finding = Finding(
        agent="energy",
        summary="观影 2 小时预计耗电 1.8 kWh",
        analyses=["按峰平谷电价折算约 ¥1.1"],
    )
    g5 = _build_stub_graph(ctx, {"scene": MOVIE_FINDING, "energy": energy_finding}, ["scene", "energy"])
    base = max_command_id()
    out5 = await runner.run("我要看电影，顺便看看要花多少电", thread_id=f"chk-multi-{RUN_TAG}",
                            ctx=ctx, graph=g5, home_id=1)
    sec5 = out5.get("answer_sections") or {}
    ck.check("两个 Agent 的结论都进了回答",
             len(sec5.get("by_agent", [])) == 2,
             "；".join(f"{b['label']}" for b in sec5.get("by_agent", [])))
    ck.check("事实去重后合并", len(sec5.get("observed_facts", [])) >= 1)
    ck.check("分析合并", len(sec5.get("analyses", [])) >= 2,
             f"{len(sec5.get('analyses', []))} 条")
    ck.check("不确定项被保留（不编造）", len(sec5.get("uncertainties", [])) >= 1)
    print()

    # ======================================================================
    # 3.6 流式事件
    # ======================================================================
    print("3.6 流式输出（SSE 事件）")
    g6 = _build_stub_graph(ctx, {"scene": MOVIE_FINDING}, ["scene"])
    events: list[dict[str, Any]] = []
    async for ev in runner.stream("我要看电影了，顺便把门锁上", thread_id=f"chk-stream-{RUN_TAG}",
                                  ctx=ctx, graph=g6, home_id=1):
        events.append(ev)
    kinds = [e.get("type") for e in events]
    ck.check("产出 node 事件（进度提示）", kinds.count("node") >= 4, f"{kinds.count('node')} 个")
    ck.check("产出 interrupt 事件（弹窗）", "interrupt" in kinds)
    ck.check("产出 final 事件", "final" in kinds)
    ck.check("没有 error 事件", "error" not in kinds,
             "; ".join(str(e.get("message")) for e in events if e.get("type") == "error"))

    node_names = [e.get("node") for e in events if e.get("type") == "node"]
    ck.check("事件顺序覆盖主干（路由→助手→汇总→编排→执行→确认）",
             "scene" in node_names and "plan" in node_names and "execute" in node_names,
             "→".join(str(n) for n in node_names))

    # ⚠️ 回归点：流式中断必须同时登记进待确认索引。
    # 只推 `interrupt` 事件不登记的话，前端卡片画得出来，点「批准」却会
    # 404（该会话没有待确认事项）—— `__interrupt__` 不在 snapshot.values 里，
    # 光靠最后的 `_normalize(values)` 是拿不到的。
    tid6 = f"chk-stream-{RUN_TAG}"
    final6 = next((e.get("result") or {} for e in events if e.get("type") == "final"), {})
    ck.check("final 事件标记了 interrupted", bool(final6.get("interrupted")))
    ck.check("final 事件带上了待确认载荷",
             bool((final6.get("intervention") or {}).get("interventions")),
             f"{len((final6.get('intervention') or {}).get('interventions') or [])} 条")
    rec6 = runner.get_pending(tid6)
    ck.check("流式中断后会话进了待确认索引（否则批准 404）", rec6 is not None)
    ck.check("索引里的载荷带 interventions",
             bool((rec6 or {}).get("payload", {}).get("interventions")))
    runner.clear_pending(tid6)


# ============================================================================
# 第 4 层：路由降级
# ============================================================================


def layer4() -> Checker:
    ck = Checker()
    print()
    print("=" * 76)
    print("4 · 路由降级（模型不可用时不能整个请求失败）")
    print("=" * 76)

    async def go() -> None:
        import app.models.llm as llm_mod
        from app.agent import router as router_mod
        from app.agent.state import initial_state

        saved = llm_mod.get_llm

        def broken_llm(*a: Any, **k: Any):
            raise RuntimeError("模拟：模型不可用（额度耗尽 / 网络故障）")

        llm_mod.get_llm = broken_llm  # type: ignore[assignment]
        try:
            out = await router_mod.route_node(initial_state("客厅现在多少度？"))
        finally:
            llm_mod.get_llm = saved  # type: ignore[assignment]

        route = out["route"]
        ck.check("模型挂了也不抛异常", route is not None)
        ck.check("降级到场景助手（覆盖面最广的那个）",
                 route.agents == ["scene"], f"agents={route.agents}")
        ck.check("降级原因写进了 reason（可复盘）", "降级" in (route.reason or ""),
                 route.reason or "")

        # 空问题：不调模型，直接反问
        out2 = await router_mod.route_node(initial_state("   "))
        ck.check("空问题直接反问，不浪费一次模型调用",
                 bool(out2["route"].needs_clarification) and not out2["route"].agents)

    asyncio.run(go())

    # ---- 路由边界与兜底的纯函数单测（不调模型，零成本防回归）----
    # 这几条是"模型一次误判不至于让系统哑掉"的最后防线，必须钉住。
    from app.agent.models import RouteDecision
    from app.agent.router import apply_route_fallbacks

    print()
    print("  -- 路由兜底规则（纯函数，不调模型）--")

    d1 = apply_route_fallbacks(
        RouteDecision(intents=["查询室温"], agents=[], reason="纯查询"))
    ck.check("🚨 空路由且无需反问 → 兜底激活助手（否则用户只拿到空答案）",
             d1.agents == ["scene"], f"agents={d1.agents}")
    ck.check("兜底原因写进了 reason（可复盘）", "兜底" in (d1.reason or ""), d1.reason or "")

    d2 = apply_route_fallbacks(
        RouteDecision(intents=[], agents=[], reason="",
                      needs_clarification="请问具体想做什么？"))
    ck.check("要反问用户时不兜底（不能一边问一边动手）",
             d2.agents == [], f"agents={d2.agents}")

    d3 = apply_route_fallbacks(
        RouteDecision(intents=[], agents=["scene", "不存在的助手"], reason=""))
    ck.check("模型编造的 Agent 名被丢弃", d3.agents == ["scene"], f"agents={d3.agents}")

    d4 = apply_route_fallbacks(
        RouteDecision(intents=[], agents=["energy"], reason="",
                      needs_clarification="先问清楚"))
    ck.check("反问与 Agent 同时给出 → 清空 agents", d4.agents == [], f"agents={d4.agents}")

    return ck


# ============================================================================
# 第 5 层：API
# ============================================================================


def layer5(base_url: str) -> Checker:
    ck = Checker()
    print()
    print("=" * 76)
    print(f"5 · API 端点  →  {base_url}")
    print("=" * 76)

    import httpx

    # ⚠️ `trust_env=False`：本机若设了 HTTP_PROXY，httpx 会把 127.0.0.1 也走代理，
    # 表现是"全部请求 502"，很容易被误判成后端挂了。
    try:
        _probe = httpx.Client(base_url=base_url.rstrip("/"), timeout=5.0, trust_env=False)
        _probe.get("/api/health")
        _probe.close()
    except Exception as e:  # noqa: BLE001
        ck.check(f"连得上 API（{base_url}）", False,
                 f"{type(e).__name__}：先跑 `python run_api.py` 再重跑本层")
        return ck

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=120.0, trust_env=False) as cli:
        # ---- 待确认列表 ----
        r = cli.get("/api/confirmations/pending")
        ck.check("GET /api/confirmations/pending 可用", r.status_code == 200,
                 f"HTTP {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            ck.check("列表结构正确（items / total）",
                     isinstance(body.get("items"), list) and "total" in body,
                     f"total={body.get('total')}")

        # ---- 不存在的会话要报 404，而不是 500 ----
        r = cli.post("/api/confirmations/no-such-thread",
                     json={"choice": "approve", "intervention_index": 0})
        ck.check("对不存在的会话给决定 → 404（不是 500）", r.status_code == 404,
                 f"HTTP {r.status_code}")

        # ---- 对话（非流式）----
        r = cli.post("/api/chat", json={"question": "客厅现在多少度？", "home_id": 1})
        ck.check("POST /api/chat 可用", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            ck.check("返回 thread_id（后续确认/追问要带着）", bool(body.get("thread_id")),
                     body.get("thread_id", ""))
            ck.check("返回结构完整",
                     all(k in body for k in ("interrupted", "answer", "sections", "trace", "route")),
                     f"keys={len(body)}")

        # ---- 对话（流式）----
        kinds: list[str] = []
        with cli.stream("POST", "/api/chat/stream",
                        json={"question": "客厅现在多少度？", "home_id": 1}) as resp:
            ck.check("POST /api/chat/stream 可用", resp.status_code == 200,
                     f"HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    import json as _json
                    try:
                        kinds.append(_json.loads(line[6:]).get("type", "?"))
                    except Exception:  # noqa: BLE001
                        pass
        ck.check("SSE 事件序列完整（start → … → done）",
                 kinds and kinds[0] == "start" and kinds[-1] == "done",
                 "→".join(kinds))

        # ---- OpenAPI 里能查到新端点 ----
        r = cli.get("/openapi.json")
        if r.status_code == 200:
            paths = r.json().get("paths", {})
            for p in ("/api/chat", "/api/chat/stream",
                      "/api/confirmations/pending", "/api/confirmations/{thread_id}"):
                ck.check(f"已注册 {p}", p in paths)

    return ck


# ============================================================================
# 第 6 层：真实模型（可选）
# ============================================================================


def layer6(ctx: ToolContext) -> Checker:
    ck = Checker()
    print()
    print("=" * 76)
    print("6 · 真实模型（路由分类 + 专业 Agent）")
    print("=" * 76)

    async def go() -> None:
        from app.agent import router as router_mod
        from app.agent.graph import build_graph
        from app.agent import runner
        from app.agent.state import initial_state

        # ---- 6.1 路由 ----
        cases = [
            ("客厅现在多少度？", {"scene"}),
            ("这个月电费为什么这么高？", {"energy"}),
            ("门锁怎么反锁？", {"assistant"}),
        ]
        for q, expect in cases:
            out = await router_mod.route_node(initial_state(q))
            agents = set(out["route"].agents)
            ck.check(f"路由「{q}」→ {sorted(expect)}", bool(agents & expect),
                     f"实际 {sorted(agents)}｜{out['route'].reason[:40]}")

        # ---- 6.2 端到端（真模型）----
        g = build_graph(ctx)
        out = await runner.run("客厅现在多少度？", thread_id=f"chk-llm-{RUN_TAG}", ctx=ctx, graph=g, home_id=1)
        sec = out.get("sections") or {}
        ck.check("真模型端到端跑通", not (out.get("interrupted") is None),
                 f"中断={out['interrupted']}")
        ck.check("产出了观测事实或不确定项",
                 bool(sec.get("observed_facts") or sec.get("uncertainties")),
                 f"事实 {len(sec.get('observed_facts', []))} 条"
                 f"／不确定 {len(sec.get('uncertainties', []))} 条")
        ck.check("有执行痕迹（可复盘）", len(out.get("trace", [])) >= 3,
                 f"{len(out.get('trace', []))} 个节点")

    try:
        asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        ck.check("真实模型链路", False, f"{type(e).__name__}: {e}")
    return ck


# ============================================================================
# 入口
# ============================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="M4 多 Agent 编排验收")
    ap.add_argument("--api", action="store_true", help="加跑第 5 层（需 API 在运行）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="API 地址")
    ap.add_argument("--with-llm", action="store_true", help="加跑第 6 层（会调用真实模型）")
    args = ap.parse_args()

    print()
    print("#" * 76)
    print("# M4 · 多 Agent 编排验收")
    print("#" * 76)

    catalog = DeviceCatalog.load(engine)
    from app.mqtt.cache import LatestValueCache

    ctx = ToolContext(catalog=catalog, cache=LatestValueCache(), home_id=1)
    print(f"  设备目录 : {len(catalog)} 台 / {len(catalog.rooms)} 个房间")
    print(f"  高风险设备: {', '.join(d.name for d in catalog.high_risk())}")
    print(f"  专业 Agent: {', '.join(AGENT_LABELS[a] for a in ALL_AGENTS)}")
    print()

    cks = [layer1(ctx), layer2(ctx), layer3(ctx), layer4()]
    if args.api:
        cks.append(layer5(args.base_url))
    if args.with_llm:
        cks.append(layer6(ctx))

    passed = sum(c.passed for c in cks)
    failures = sum(len(c.failed) for c in cks)

    print()
    print("=" * 76)
    if failures:
        print(f"结果：{passed} PASS / {failures} FAIL")
    else:
        print(f"结果：{passed} PASS / 0 FAIL  —— 多 Agent 编排验收通过 ✅")
    print("=" * 76)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
