"""图状态定义。

并行合并的关键
------------
`findings` 与 `trace` 用 `Annotated[list, operator.add]` 声明 reducer：
LangGraph 在多个并行分支都返回该通道时，会**逐条累加**而不是互相覆盖。

⚠️ 只有需要并行合并的通道才加 reducer。`route` / `plan` / `run_result`
这些是"单写者"通道（只有一个节点会写），加了 reducer 反而会让重复执行时叠加出脏数据。

状态里存 Pydantic 对象而不是 dict
------------------------------
LangGraph 默认的序列化器支持 Pydantic v2 模型，而对象比 dict 更安全
（字段名写错会在构造时报错，而不是在几层之后才炸）。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from app.agent.models import (
    Decision,
    Fact,
    Finding,
    InterventionItem,
    ProposedAction,
    RouteDecision,
)


class TraceEntry(TypedDict):
    """一个节点的执行痕迹。给前端"正在做什么"用，也是排障的第一手材料。"""

    node: str
    label: str
    detail: str
    elapsed_ms: int


class AgentState(TypedDict, total=False):
    """主图状态。

    分五段：输入 → 路由 → 并行产出 → 确定性主干 → 输出。
    """

    # ---------------- 输入 ----------------
    question: str
    home_id: int
    #: 发言人。首期为单用户（户主），多成员审批留到第二阶段
    member_name: str | None
    thread_id: str

    # ---------------- 路由（单写者）----------------
    route: RouteDecision | None

    # ---------------- 并行产出（多写者，需要 reducer）----------------
    findings: Annotated[list[Finding], operator.add]
    trace: Annotated[list[TraceEntry], operator.add]

    # ---------------- 确定性主干（单写者）----------------
    #: 提议的动作（各 Agent 的 proposed_actions 汇总后的原始形态，未经校验）
    proposed_actions: list[ProposedAction]
    #: M3 编排结果：能力校验 + 冲突检测后的计划
    plan: dict[str, Any] | None
    #: 计划里被剔除的动作及原因（规划书 §14.1 要求"能复现"）
    plan_rejected: list[dict[str, Any]]

    # ---------------- 人工介入 ----------------
    #: 待人工决定的事项（高风险确认 / 冲突裁决共用）
    interventions: list[InterventionItem]
    #: 人给出的决定（`Command(resume=...)` 注入）
    decisions: list[Decision]
    #: 本次是否发生过中断（用于前端展示与落库 status=interrupted）
    interrupted: bool

    # ---------------- 执行 ----------------
    run_result: dict[str, Any] | None

    # ---------------- 输出 ----------------
    #: 分区回答（观测事实 / 分析结果 / 记忆依据 / 待确认项），对应规划书 §10.2
    answer_sections: dict[str, Any]
    #: 一句话总结（给前端做气泡首行）
    answer: str


def initial_state(
    question: str,
    *,
    home_id: int = 1,
    member_name: str | None = None,
    thread_id: str = "default",
) -> AgentState:
    """构造一张新图的初始状态。

    ⚠️ 带 reducer 的通道（findings / trace）必须显式给空列表，
    否则在部分 LangGraph 版本下首次合并会缺默认值。
    """
    return AgentState(
        question=question,
        home_id=home_id,
        member_name=member_name,
        thread_id=thread_id,
        route=None,
        findings=[],
        trace=[],
        proposed_actions=[],
        plan=None,
        plan_rejected=[],
        interventions=[],
        decisions=[],
        interrupted=False,
        run_result=None,
        answer_sections={},
        answer="",
    )


def collect_proposed_actions(findings: list[Finding]) -> list[ProposedAction]:
    """把各 Agent 提议的动作汇总成一个列表（保持 Agent 出场顺序）。"""
    out: list[ProposedAction] = []
    for f in findings:
        out.extend(f.proposed_actions)
    return out


def collect_facts(findings: list[Finding]) -> list[Fact]:
    """汇总所有观测事实（去重：同一 device_code + label 只留第一条）。"""
    seen: set[tuple[str | None, str]] = set()
    out: list[Fact] = []
    for f in findings:
        for fact in f.facts:
            key = (fact.device_code, fact.label)
            if key in seen:
                continue
            seen.add(key)
            out.append(fact)
    return out
