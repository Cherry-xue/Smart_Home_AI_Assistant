"""确定性主干上的节点：汇总 → 编排 → 低风险执行 → 高风险确认 → 高风险执行 → 回答。

这些节点**都不调用模型**。它们承担 M4 里"确定性代码"的部分，与 M3 一脉相承：

    模型负责"想做什么"，代码负责"能不能做、做了会怎样"。

⚠️ 两个关键设计
------------
1. **`ctx` 用闭包捕获，不放图状态**。图状态会被 checkpoint 序列化，
   把数据库 engine、缓存对象塞进去既没用又会出问题（无法反序列化）。
2. **确认节点的位置在汇聚之后的主干上**，不放进并行的专业 Agent 分支 ——
   `interrupt()` 在并行分支中的行为没有明确定义（多分支同时中断时合并语义不清）。

关于"低风险直接执行"的落地方式（用户 2026-10-15 确认的要求）
--------------------------------------------------------
把执行拆成**两段**：

    低风险 → 立刻执行 → 高风险才中断等确认 → 恢复后执行被批准的高风险

而不是"全部挂起、恢复后一起执行"。区别在于：用户迟迟不确认时，
**低风险动作已经生效了** —— 这才符合"其他直接执行，执行完告知"。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import time
from typing import Any, Callable, Coroutine

from app.agent import AGENT_LABELS
from app.agent.models import Decision, InterventionItem
from app.agent.state import (
    AgentState,
    TraceEntry,
    collect_facts,
    collect_proposed_actions,
)
from app.db.catalog import DeviceCatalog
from app.services.scenes import planner as scene_planner
from app.services.scenes.models import ScenePlan, StepOutcome

log = logging.getLogger("app.agent.nodes")

#: 高风险动作确认的有效期（分钟）。超时未确认则失效，
#: 避免"用户没看手机 → 这个会话永远悬着"。
CONFIRM_TTL_MIN = 10

#: 视为"同意"的选择值（宽容匹配前端可能传来的各种写法）
_APPROVE_TOKENS = {"approve", "approved", "yes", "ok", "同意", "确认", "批准"}


def _trace(node: str, label: str, started: float, detail: str) -> TraceEntry:
    return TraceEntry(
        node=node,
        label=label,
        detail=detail,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


# ============================================================================
# 汇总：纯代码拼接，不再调用模型
# ============================================================================


async def aggregate_node(state: AgentState) -> dict:
    """把各 Agent 的 `Finding` 拼成"回答分区"。

    ⚠️ 这里**刻意不调用模型**。让模型"再总结一遍"是信息二次损耗，而且多一次
    幻觉机会。结构化之后汇总退化成字段拼接：可测、可复现，
    事实与建议天然分开（正好满足规划书 §10.2 的回答分区要求）。
    """
    started = time.perf_counter()
    findings = state.get("findings") or []

    facts = collect_facts(findings)
    analyses: list[str] = []
    memory_refs: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    uncertainties: list[str] = []
    summaries: list[str] = []

    seen_mem: set[str] = set()
    seen_cite: set[tuple[str, str]] = set()

    for f in findings:
        label = AGENT_LABELS.get(f.agent, f.agent)
        if f.summary:
            summaries.append(f"{label}：{f.summary}")
        analyses.extend(f.analyses)
        uncertainties.extend(f.uncertainties)
        for m in f.memory_refs:
            key = f"{m.memory_id}:{m.content}"
            if key in seen_mem:
                continue
            seen_mem.add(key)
            memory_refs.append(m.model_dump())
        for c in f.citations:
            key = (c.source_file, c.section)
            if key in seen_cite:
                continue
            seen_cite.add(key)
            citations.append(c.model_dump())

    actions = collect_proposed_actions(findings)

    sections: dict[str, Any] = {
        "observed_facts": [x.render() for x in facts],
        "analyses": analyses,
        "memory_refs": memory_refs,
        "citations": citations,
        "uncertainties": uncertainties,
        "suggestions": summaries,
        "pending": [],
        "by_agent": [
            {
                "agent": f.agent,
                "label": AGENT_LABELS.get(f.agent, f.agent),
                "summary": f.summary,
                "action_count": len(f.proposed_actions),
            }
            for f in findings
        ],
    }

    detail = f"合并 {len(findings)} 个结论：{len(facts)} 条事实、{len(actions)} 个待编排动作"
    return {
        "answer_sections": sections,
        "proposed_actions": actions,
        "trace": [_trace("aggregate", "汇总结论", started, detail)],
    }


# ============================================================================
# 编排：复用 M3 的确定性 planner
# ============================================================================


def build_plan_node(ctx: Any) -> Callable[[AgentState], Coroutine[Any, Any, dict]]:
    """构造编排节点（能力校验 → 去重 → 意图约束 → 风险标记）。"""
    catalog: DeviceCatalog = ctx.catalog

    async def plan_node(state: AgentState) -> dict:
        started = time.perf_counter()
        actions = state.get("proposed_actions") or []

        if not actions:
            return {
                "plan": None,
                "plan_rejected": [],
                "trace": [_trace("plan", "校验动作", started, "没有需要编排的动作")],
            }

        candidates = [
            {"device_code": a.device_code, "capability": a.capability, "value": a.value}
            for a in actions
        ]

        # 按当前套房收窄设备目录：对话里说"把灯关了"，必须是**这一套房**的灯。
        # ⚠️ 每次进入节点都现算：ctx.catalog 会被 refresh() 就地更新，而图是按进程
        #    缓存的，提前算好就会永远停在旧快照上（新加的设备永远进不来）。
        home_catalog = catalog.for_home(state.get("home_id"))

        try:
            plan: ScenePlan = scene_planner.plan_scene(
                home_catalog,
                candidates,
                scene_name=(state.get("question") or "")[:60],
            )
        except Exception as e:  # noqa: BLE001
            log.exception("编排失败")
            sections = dict(state.get("answer_sections") or {})
            sections["uncertainties"] = [
                *(sections.get("uncertainties") or []),
                f"动作编排失败，未生成任何可执行计划：{type(e).__name__}",
            ]
            return {
                "plan": None,
                "plan_rejected": [],
                "answer_sections": sections,
                "trace": [_trace("plan", "校验动作", started, f"编排失败 {type(e).__name__}")],
            }

        plan_dict = {
            "intent_key": plan.intent_key,
            "intent_label": plan.intent_label,
            "raw_count": plan.raw_count,
            "notes": list(plan.notes),
            "actions": [s.as_dict() for s in plan.actions],
            "rejected": [s.as_dict() for s in plan.rejected],
        }
        high_risk = [s for s in plan.actions if s.high_risk_device]
        detail = (
            f"{plan.raw_count} 个候选 → 保留 {len(plan.actions)}"
            f"（剔除 {len(plan.rejected)}，其中高风险 {len(high_risk)}）"
        )
        log.info("编排完成：%s", detail)
        return {
            "plan": plan_dict,
            "plan_rejected": plan_dict["rejected"],
            "trace": [_trace("plan", "校验与冲突检测", started, detail)],
        }

    return plan_node


# ============================================================================
# 中断：整张图里唯一会"停住"的地方
# ============================================================================


def build_confirm_gate(ctx: Any):
    """高风险确认节点（`interrupt()` 在这里发生）。

    与用户的约定（2026-10-15）：
    - 低风险动作**直接执行**，执行完告知；
    - 高风险动作弹窗确认；
    - "省电还是舒适"这类冲突裁决**共用同一条通道**（`kind="conflict"`）。

    合并成一条通道的理由：两者本质都是"暂停 → 等人决定 → 继续"，
    可以共用同一套前端组件与恢复逻辑，将来加第三种介入也不用重建。

    ⚠️ 为什么在 `interrupt()` **之前**就把指令写进库
    --------------------------------------------
    早先的版本是"等批准之后再写库"，结果中断期间数据库里**没有任何痕迹**，
    待确认列表只能靠进程内存维护 —— 进程一重启，弹窗就消失了。

    现在改成：生成待确认项时就落一条 `pending_confirm` 指令（带 `thread_id`
    与 `intervention_index`），并把 `command_id` 回填到待确认项上。这样：
    - 手机端的待确认列表可以**从库里重建**（`source='agent'` + `thread_id`），不再依赖内存；
    - 每个待确认项都有主键可反查，批准/驳回是**更新**那一行，而不是再插一条（不重复骚扰）；
    - 与 M3 的 `/scenes/{id}/run` 行为一致（它也是先落待确认再等人）。
    """

    async def confirm_gate_node(state: AgentState) -> dict:
        from langgraph.types import interrupt

        started = time.perf_counter()

        # 已经从 resume 恢复进来（decisions 非空）→ 放行
        if state.get("decisions"):
            return {
                "interrupted": True,
                "trace": [
                    _trace("confirm", "人工确认", started,
                           f"已收到人工决定（{len(state.get('decisions') or [])} 条），继续")
                ],
            }

        plan = state.get("plan") or {}
        high_risk = [a for a in (plan.get("actions") or []) if a.get("high_risk_device")]

        if not high_risk:
            return {
                "interrupted": False,
                "trace": [
                    _trace("confirm", "人工确认", started,
                           "全部为低风险动作，无需确认，直接执行")
                ],
            }

        expires = dt.datetime.now() + dt.timedelta(minutes=CONFIRM_TTL_MIN)
        command_ids = _persist_pending(ctx, plan, state.get("thread_id"))

        items = [
            InterventionItem(
                kind="high_risk",
                device_code=a.get("device_code"),
                device_name=a.get("device_name") or "",
                capability=a.get("capability"),
                value=a.get("value"),
                prompt=(
                    f"是否允许 {a.get('room_name') or ''}{a.get('device_name')} "
                    f"执行「{a.get('capability')} = {a.get('value')}」？"
                ),
                detail=a.get("reason") or "该设备属于高风险设备，必须人工确认后才会执行。",
                options=["approve", "reject"],
                command_id=command_ids.get(i),
                expires_at=expires.isoformat(timespec="seconds"),
            )
            for i, a in enumerate(high_risk)
        ]

        payload = {
            "interventions": [i.model_dump() for i in items],
            "expires_at": expires.isoformat(timespec="seconds"),
            "message": f"有 {len(items)} 个高风险动作需要你确认（{CONFIRM_TTL_MIN} 分钟内有效）",
        }

        log.info("触发人工确认，等待批准：%s", [i.device_name for i in items])
        # ⚠️ 这一行**真正暂停整张图**，把控制权交还调用方。
        # 恢复时它的返回值就是调用方传入的 `Command(resume=...)`。
        resumed = interrupt(payload)

        decisions = _parse_resume(resumed)
        return {
            "interventions": items,
            "decisions": decisions,
            "interrupted": True,
            "trace": [
                _trace("confirm", "人工确认", started,
                       f"收到 {len(decisions)} 条决定："
                       + ", ".join(f"{d.choice}" for d in decisions))
            ],
        }

    return confirm_gate_node


def _persist_pending(ctx: Any, plan_dict: dict[str, Any], thread_id: str | None) -> dict[int, int]:
    """为计划里的高风险动作各落一条 `pending_confirm` 指令。

    返回 `{高风险动作序号: command_id}`。写库失败**不阻断中断**
    （弹窗是给人的，不能因为数据库抖一下就不弹）—— 失败时返回空字典，
    后续批准会退化成"临时补写一条"。

    ⚠️ 必须是**幂等**的
    ------------------
    `Command(resume=...)` 恢复时，LangGraph 会**重新执行**被中断的节点
    （`interrupt()` 这次直接返回恢复值，不再暂停）。也就是说本函数会被跑两遍。
    不幂等的话，批准一次就多一条待办，手机端会看到重复项。

    幂等的判据是"同一会话 + 同一设备 + 同一能力 + 仍是待确认"：命中就复用，不新建。
    """
    from sqlalchemy import select

    from app.models.orm import DeviceCommand
    from app.services.scenes import executor as scene_executor

    catalog: DeviceCatalog = ctx.catalog
    # 待确认指令也只能挂在本套房的设备上（for_home(None) 会原样返回，等于不过滤）
    catalog = catalog.for_home(getattr(ctx, "home_id", None))
    plan = _rebuild_plan(plan_dict)
    high_risk = [a for a in plan.actions if a.high_risk_device]
    if not high_risk:
        return {}

    out: dict[int, int] = {}
    try:
        db = _session_factory()()
        try:
            for i, outcome in enumerate(high_risk):
                device = catalog.get(outcome.device_code)
                if device is None:
                    continue

                existing = _find_pending(db, DeviceCommand, device.id, outcome.capability, thread_id)
                if existing is not None:
                    log.info(
                        "复用已存在的待确认指令 #%s（%s.%s）—— 恢复时节点会重跑，不能重复插",
                        existing.id, outcome.device_code, outcome.capability,
                    )
                    out[i] = existing.id
                    outcome.command_id = existing.id
                    continue

                record = scene_executor.create_command(
                    db, device, outcome,
                    status="pending_confirm",
                    risk_level="high",
                    result_message="高风险设备：需人工确认后执行（当前仅登记，未下发）",
                    issued_by="agent",
                    extra_params={
                        "source": "agent",
                        "thread_id": thread_id,
                        "intervention_index": i,
                    },
                )
                out[i] = record.id
                outcome.command_id = record.id
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        log.exception("待确认指令落库失败（不阻断弹窗）")
    return out


def _find_pending(db, model, device_id: int, capability: str, thread_id: str | None):
    """找同一会话下已存在的待确认指令（用于幂等复用）。"""
    from sqlalchemy import select

    rows = (
        db.execute(
            select(model)
            .where(
                model.device_id == device_id,
                model.command == capability,
                model.status == "pending_confirm",
            )
            .order_by(model.id.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    if not thread_id:
        return rows[0] if rows else None
    for r in rows:
        params = r.params_json
        if isinstance(params, str):
            # PyMySQL 有时把 JSON 列原样返回成字符串
            try:
                params = json.loads(params)
            except Exception:  # noqa: BLE001
                params = {}
        if (params or {}).get("thread_id") == thread_id:
            return r
    return None


def _parse_resume(resumed: Any) -> list[Decision]:
    """把 `Command(resume=...)` 传来的内容解析成 `Decision` 列表。

    宽容解析：前端可能传 `{"decisions": [...]}`、单个 dict，或直接传列表。
    """
    if resumed is None:
        return []
    raw = resumed.get("decisions") if isinstance(resumed, dict) else resumed
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    out: list[Decision] = []
    for i, item in enumerate(raw):
        if isinstance(item, Decision):
            out.append(item)
            continue
        if isinstance(item, dict):
            try:
                out.append(
                    Decision(
                        intervention_index=int(item.get("intervention_index", i)),
                        choice=str(item.get("choice") or item.get("decision") or ""),
                        note=str(item.get("note") or ""),
                    )
                )
            except Exception:  # noqa: BLE001
                log.warning("无法解析的决定项：%r", item)
    return out


# ============================================================================
# 执行：复用 M3 的 executor（含重试 / 降级 / 高风险强制 pending）
# ============================================================================


def build_execute_node(ctx: Any, *, only_high_risk: bool):
    """构造执行节点。

    - `only_high_risk=False`：执行计划里的**低风险**动作（中断之前就跑掉）；
    - `only_high_risk=True` ：只执行**被批准**的高风险动作。

    ⚠️ 即使调用方说"批准了"，`executor.create_command()` 里的强制代码仍会把
    高风险指令写成 `pending_confirm` —— 除非显式打开 `HIGH_RISK_AUTO_EXEC`。
    两道锁是独立的：这里控制"发不发"，那里控制"记为已执行还是待确认"。
    """
    catalog: DeviceCatalog = ctx.catalog
    home_id: int = getattr(ctx, "home_id", 1)

    async def execute_node(state: AgentState) -> dict:
        started = time.perf_counter()
        # 执行路径同样只认本套房的设备。现算而不缓存的原因同 plan_node：
        # ctx.catalog 会被 refresh() 就地更新，图却是按进程缓存的。
        home_catalog = catalog.for_home(home_id)
        plan_dict = state.get("plan")

        if not plan_dict or not plan_dict.get("actions"):
            return {
                "run_result": state.get("run_result"),
                "trace": [_trace("execute", "执行动作", started, "没有可执行的动作")],
            }

        plan = _rebuild_plan(plan_dict)
        decisions = state.get("decisions") or []

        if only_high_risk:
            return _settle_high_risk(state, plan, decisions, started)
        else:
            targets = [a for a in plan.actions if not a.high_risk_device]
            if not targets:
                return {
                    "trace": [_trace("execute", "执行低风险动作", started, "没有低风险动作")],
                }

        sub_plan = ScenePlan(
            intent_key=plan.intent_key,
            intent_label=plan.intent_label,
            raw_count=len(targets),
            notes=list(plan.notes),
        )
        sub_plan.actions = targets
        sub_plan.rejected = []

        from app.services.scenes import executor as scene_executor

        try:
            db = _session_factory()()
            try:
                result = scene_executor.execute_plan(
                    sub_plan, home_catalog, db=db, home_id=home_id,
                    issued_by="agent", dry_run=False,
                    # 带上会话标识：手机端的"待确认列表"要靠它反查是哪个会话挂起的
                    extra_params={"source": "agent", "thread_id": state.get("thread_id")},
                )
                # ⚠️ 刻意**不写 `scene_runs`**：那张表是"场景执行"的记录，
                # 而 Agent 触发的动作不属于任何场景（`scene_id` 是 NOT NULL，写不进去）。
                # 留痕由两处承担：`device_commands` 逐条指令 + `agent_runs` 整轮对话。
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            log.exception("执行失败")
            note = f"执行异常：{type(e).__name__}: {e}"
            prev = state.get("run_result") or {}
            return {
                "run_result": {**prev, "status": prev.get("status") or "failed", "notes": [*prev.get("notes", []), note]},
                "trace": [_trace("execute", "执行动作", started, note)],
            }

        detail = (
            f"{'高风险' if only_high_risk else '低风险'}动作："
            f"执行 {len(result.executed)}，待确认 {len(result.pending_confirm)}，"
            f"失败 {len(result.failed)}（{result.status}）"
        )
        log.info("执行完成：%s", detail)

        # 与之前那一段执行结果合并（低风险先跑，高风险后跑）
        prev = state.get("run_result") or {}
        merged = {
            "status": _merge_status(prev.get("status"), result.status),
            "run_id": result.run_id or prev.get("run_id"),
            "executed": [*prev.get("executed", []), *[s.as_dict() for s in result.executed]],
            "pending_confirm": [
                *prev.get("pending_confirm", []), *[s.as_dict() for s in result.pending_confirm]
            ],
            "failed": [*prev.get("failed", []), *[s.as_dict() for s in result.failed]],
            "notes": [*prev.get("notes", []), *list(result.notes)],
        }
        return {
            "run_result": merged,
            "trace": [_trace("execute", "执行动作", started, detail)],
        }

    return execute_node


def _settle_high_risk(
    state: AgentState, plan: ScenePlan, decisions: list[Any], started: float
) -> dict:
    """把人工决定落到**已经存在**的待确认指令上。

    ⚠️ 这里刻意**不调用 `execute_plan`**：
    待确认指令在 `confirm_gate` 中断前就已经落库了（`_persist_pending`），
    再走一遍执行会变成"批准一次、插两条指令"，手机端就会看到重复的待办。

    而"批准"能做的事也只有留痕 —— 红线规定高风险设备**永不自动执行**
    （`HIGH_RISK_AUTO_EXEC=false`），所以批准后的状态仍然是 `pending_confirm`，
    只是补上了 `confirmed_at` 与说明。真正的动作仍须人到现场或走授权通道。
    """
    from app.models.orm import DeviceCommand

    high_risk = [a for a in plan.actions if a.high_risk_device]
    if not high_risk:
        return {"trace": [_trace("execute", "执行已确认动作", started, "没有高风险动作")]}

    interventions = state.get("interventions") or []
    notes: list[str] = []
    still_pending: list[StepOutcome] = []
    now = dt.datetime.now()

    try:
        db = _session_factory()()
        try:
            for i, outcome in enumerate(high_risk):
                choice = _choice_for(decisions, i)
                cmd_id = _command_id_of(interventions, i) or outcome.command_id
                row = db.get(DeviceCommand, cmd_id) if cmd_id else None

                if choice in _APPROVE_TOKENS:
                    notes.append(
                        f"已人工确认「{outcome.device_name} {outcome.capability}」；"
                        "⚠️ 高风险动作不会自动执行，实际动作需人工或授权通道完成"
                    )
                    still_pending.append(outcome)
                    if row is not None:
                        row.confirmed_at = now
                        row.result_message = (
                            "已人工确认；高风险动作不自动执行（HIGH_RISK_AUTO_EXEC=false）"
                        )
                elif choice:
                    notes.append(
                        f"已驳回「{outcome.device_name} {outcome.capability}」，该动作不会执行"
                    )
                    if row is not None:
                        row.status = "rejected"
                        row.result_message = "人工驳回"
                else:
                    notes.append(
                        f"未收到对「{outcome.device_name} {outcome.capability}」的决定，保持待确认"
                    )
                    still_pending.append(outcome)
                    if row is not None:
                        row.result_message = "未获得人工确认，保持待确认"
            db.commit()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        log.exception("回写人工决定失败")
        notes.append(f"回写人工决定失败：{type(e).__name__}: {e}")

    detail = (
        f"高风险动作：{len(high_risk)} 项待确认 → 批准后仍需人工执行 {len(still_pending)} 项"
        f"（共 {len([d for d in decisions])} 条决定）"
    )

    prev = state.get("run_result") or {}
    merged = {
        "status": _merge_status(prev.get("status"), "success"),
        "run_id": prev.get("run_id"),
        "executed": list(prev.get("executed", [])),
        "pending_confirm": [
            *prev.get("pending_confirm", []), *[s.as_dict() for s in still_pending]
        ],
        "failed": list(prev.get("failed", [])),
        "notes": [*prev.get("notes", []), *notes],
    }
    return {
        "run_result": merged,
        "trace": [_trace("execute", "执行已确认动作", started, detail)],
    }


def _choice_for(decisions: list[Any], index: int) -> str:
    """取第 `index` 条介入事项的决定（小写）。没决定则返回空串。"""
    for d in decisions:
        if int(getattr(d, "intervention_index", 0) or 0) == index:
            return str(getattr(d, "choice", "") or "").strip().lower()
        if isinstance(d, dict) and int(d.get("intervention_index", 0) or 0) == index:
            return str(d.get("choice") or "").strip().lower()
    return ""


def _command_id_of(interventions: list[Any], index: int) -> int | None:
    """取第 `index` 条待确认项关联的指令主键。

    经过 checkpoint 往返后，`InterventionItem` 可能变回 dict —— 两种形态都要认。
    """
    if index >= len(interventions):
        return None
    item = interventions[index]
    if isinstance(item, dict):
        return item.get("command_id")
    return getattr(item, "command_id", None)


def _session_factory():
    from app.db.session import SessionLocal

    return SessionLocal


def _merge_status(prev: str | None, cur: str) -> str:
    """合并两次执行的状态。有任何失败就是 partial；两次都成功才是 success。"""
    if prev in ("failed", "partial") or cur in ("failed", "partial"):
        return "partial"
    return cur


def _rebuild_plan(plan_dict: dict[str, Any]) -> ScenePlan:
    """从图状态里的 dict 还原 `ScenePlan`。"""
    plan = ScenePlan(
        intent_key=plan_dict.get("intent_key") or "default",
        intent_label=plan_dict.get("intent_label") or "",
        raw_count=int(plan_dict.get("raw_count") or 0),
    )
    plan.notes = list(plan_dict.get("notes") or [])
    for a in plan_dict.get("actions") or []:
        plan.actions.append(StepOutcome(**_keep_fields(StepOutcome, a)))
    for r in plan_dict.get("rejected") or []:
        plan.rejected.append(StepOutcome(**_keep_fields(StepOutcome, r)))
    return plan


def _keep_fields(cls: Any, data: dict[str, Any]) -> dict[str, Any]:
    """过滤掉 dataclass 不认识的键（`as_dict()` 可能带出计算字段）。"""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


# ============================================================================
# 回答：拼最终输出（分区展示）
# ============================================================================


async def respond_node(state: AgentState) -> dict:
    """生成给用户看的回答。

    ⚠️ 这里**不调模型**。回答内容完全由结构化结论 + 执行结果拼成 ——
    已经查到的数据不需要模型再复述一遍（复述只会引入偏差）。
    """
    started = time.perf_counter()
    sections: dict[str, Any] = dict(state.get("answer_sections") or {})
    route = state.get("route")
    run = state.get("run_result")
    plan = state.get("plan") or {}

    rejected = state.get("plan_rejected") or []
    if rejected:
        sections["rejected"] = [
            {
                "device_name": r.get("device_name") or r.get("device_code"),
                "capability": r.get("capability"),
                "value": r.get("value"),
                "reason": r.get("reason"),
            }
            for r in rejected
        ]

    pending: list[str] = list(sections.get("pending") or [])
    if run:
        for s in run.get("pending_confirm") or []:
            pending.append(
                f"{s.get('device_name')} 的 {s.get('capability')}={s.get('value')} 需人工确认后执行"
            )
    if plan.get("notes"):
        sections["plan_notes"] = plan["notes"]
    sections["pending"] = pending

    # 一句话摘要
    #
    # ⚠️ 按**实际数量**组织措辞，不按 `status` —— 高风险动作批准后 `status` 也是
    # success，但 `executed` 是空的（红线：不自动执行）。若按 status 判断，
    # 会说出"已完成 0 个动作"这种让人困惑的话。
    executed = (run or {}).get("executed") or []
    failed = (run or {}).get("failed") or []

    if route and route.needs_clarification:
        answer = route.needs_clarification
    elif executed:
        # ⚠️ 措辞必须诚实：**下发成功 ≠ 设备状态真的变了**。
        # 曾经写死"已完成 N 个动作"，但虚拟设备运行时没启动时指令根本没人接收，
        # 界面上灯还是关的，回答却说"已完成" —— 这就是谎报。
        # 现在如实说"已下发"，并说明状态要等设备回报。
        names = "、".join(
            str(a.get("device_name") or a.get("device_code") or "")
            for a in executed[:3]
        )
        answer = f"已向 {len(executed)} 个设备下发指令（{names}），设备回报后状态即可见。"
        if failed:
            answer += f"另有 {len(failed)} 个下发失败（多为设备离线或 broker 未运行）。"
        if pending:
            answer += f"另有 {len(pending)} 项高风险动作需要你确认。"
    elif failed:
        answer = f"{len(failed)} 个动作下发失败（多为设备离线或 broker 未运行）。"
        if pending:
            answer += f"另有 {len(pending)} 项高风险动作需要你确认。"
    elif pending:
        answer = f"有 {len(pending)} 个高风险动作需要你确认后才会执行。"
    elif sections.get("suggestions"):
        answer = sections["suggestions"][0]
    else:
        answer = "已处理完成。"

    sections["answer"] = answer
    sections["route"] = {
        "agents": list(route.agents) if route else [],
        "labels": [AGENT_LABELS.get(a, a) for a in (route.agents if route else [])],
        "reason": route.reason if route else "",
        "rooms": list(route.rooms) if route else [],
    }

    return {
        "answer": answer,
        "answer_sections": sections,
        "trace": [_trace("respond", "生成回答", started, "已按分区整理结论")],
    }
